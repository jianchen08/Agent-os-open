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
use std::time::{Duration, SystemTime};

use tracing::{debug, error, warn};

use agentos_hooks::{EventTarget, HookEventBus, LifecycleEvent};

use agentos_core::traits::{PluginInvoker, StorageBackend};
use agentos_core::types::{
    ContentLoader, EngineError, PluginContext, PluginError, PluginResult, RouteNext, TenantContext,
};

use crate::compiler::{
    CompiledBody, CompiledItem, CompiledPipeline, CompiledRoute, CompiledStep, HookScope,
};
use crate::condition::eval_expr;
use crate::template::{render_template, render_value};

/// 轮数硬上限缺省值（引擎层兜底，见 [`PipelineExecutor::with_max_rounds`]）。
const DEFAULT_MAX_ROUNDS: usize = 200;

/// 持久化失败重试退避序列（毫秒）：失败后再试 2 次，仍失败则错误上抛。
const PERSIST_RETRY_BACKOFF_MS: &[u64] = &[100, 300];

/// 「终态未落库」补偿标记键（B5）：update_run_status 重试耗尽后 best-effort
/// 写入 pipeline_state 表，让 G8 重启排空兜底能按 run_id 精准识别补写，
/// 而非只靠长期 running 异常扫描。内核自有 per-run 键（VOLATILE_RUN_KEYS
/// 剥离集内）——只做观测面，不进 checkpoint、不回流下一轮 state。
const TERMINAL_PERSIST_FAILED_KEY: &str = "terminal_persist_failed";

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
    /// step 顶层键首触实录：key → 写前旧值（None = step 内新增键）。
    /// 与 ops_ledger 同边界清空/消费，替代 step 前 state 全量克隆算 diff
    /// （内核内 state 只借用不克隆；克隆仅存在于内核→进程边界的序列化点）。
    step_key_journal: parking_lot::Mutex<HashMap<String, Option<serde_json::Value>>>,
    /// 声明了 `on_pipeline_end` 生命周期钩子的插件 id（manifest 收集）。
    ///
    /// run 结束时逐个 best-effort 分发 [`LifecycleHook::OnPipelineEnd`]（HookContext
    /// 携带 pipeline_id/run_id 标签）——spill_guard 的原文清理（spill_retrieve
    /// sidecar 收通知删 `{base}/{pipeline_id}/`）依赖此通道。空集零开销。
    pipeline_end_hooks: Vec<String>,
    /// 轮次观察点（DSH 形态：循环体一次迭代 = 一轮消息；见 [`crate::round_events`]）。
    /// None = 零开销（非聊天路径/测试默认不接线）。
    round_events: Option<Arc<dyn crate::round_events::RoundEvents>>,
    /// 轮次序号（per-run 从 1 递增，fetch_add 后取 1 基）。
    round_counter: AtomicI64,
    /// 生命周期事件总线（观察路径）：run 开始/结束广播 OnPipelineStart/OnPipelineEnd
    /// 给审计/指标等订阅者。None = 零开销（未接线不发射）。
    hook_bus: Option<Arc<HookEventBus>>,
    /// 轮数硬上限（per-run 全部循环体的 while 轮累计）：while 条件恒真且终止
    /// 检查插件持续失败时的引擎层兜底，超限按 fail-closed 终止 run。0 = 非法
    /// 配置（run 入口报错拒绝）；缺省 [`DEFAULT_MAX_ROUNDS`]。
    max_rounds: usize,
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
            step_key_journal: parking_lot::Mutex::new(HashMap::new()),
            pipeline_end_hooks: Vec::new(),
            round_events: None,
            round_counter: AtomicI64::new(0),
            hook_bus: None,
            max_rounds: DEFAULT_MAX_ROUNDS,
        }
    }

    /// 注入轮数硬上限（per-run 全部循环体的 while 轮累计）。
    ///
    /// while 条件恒真且终止检查插件持续失败（插件错误统一 warn+continue）时，
    /// 主轮循环失去插件侧终止信号——引擎层硬上限是最后的兜底：超限按
    /// fail-closed 终止 run（显式错误，非静默 break）。`0` 非法（run 入口
    /// 报错拒绝，禁止"无限轮"配置）；不调用用缺省 [`DEFAULT_MAX_ROUNDS`]。
    pub fn with_max_rounds(mut self, max_rounds: usize) -> Self {
        self.max_rounds = max_rounds;
        self
    }

    /// 注入生命周期事件总线（观察路径）。
    ///
    /// run 开始/结束时把 OnPipelineStart/OnPipelineEnd 广播给订阅者（审计日志 /
    /// `lifecycle.pipeline_*_total` 指标）——点对点钩子分发（权威路径）不变，
    /// 总线只承载观察。None（不调用）零开销不发射。
    pub fn with_hook_bus(mut self, bus: Option<Arc<HookEventBus>>) -> Self {
        self.hook_bus = bus;
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

    /// 注入轮次观察点（聊天路径：api 层桥接为 stream_start/new_message/stream_end）。
    ///
    /// 不调用为零开销（引擎不感知事件协议，仅回调）。
    pub fn with_round_events(
        mut self,
        round_events: Arc<dyn crate::round_events::RoundEvents>,
    ) -> Self {
        self.round_events = Some(round_events);
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
        // 轮数硬上限非法值检查（fail-closed）：0 = 无限轮，拒绝执行。
        if self.max_rounds == 0 {
            return Err(EngineError::Config {
                message: "max_rounds=0 非法（0 = 无限轮已被禁用；须为正整数）".to_string(),
            });
        }
        let mut state = initial_state;
        ensure_object(&mut state);
        // 窗口账残留清零：跨轮复用 executor 时，上一轮死于窗口之间（Err 提前
        // 返回）的未冲洗 journal/实录不得混入本轮 run_init 轨迹。
        self.step_key_journal.lock().clear();
        self.ops_ledger.lock().clear();
        // 起始基线写入同样进轨迹（凡 state 变更必可追溯，ADR 2026-09-11）：
        // 首 run 为新增键；挂起恢复后的续 run 里 run_started_at 覆盖写是真实变更。
        if !key_present(&state, "ended") {
            self.journal_top_key(&state, "ended");
            set_key(&mut state, "ended", serde_json::Value::Bool(false));
        }
        // 控制状态键契约（ADR 2026-08-30）：run 起始墙钟，track 等插件的耗时
        // 锚点。每 run 覆盖写；挂起恢复后的新 run 从恢复点重算。
        self.journal_top_key(&state, "run_started_at");
        set_key(
            &mut state,
            "run_started_at",
            serde_json::json!(chrono::Utc::now().to_rfc3339()),
        );
        // 清空上一轮残留的插件错误收集（registry 热路径跨轮复用 state——
        // 不清理会让上一轮的 _plugin_errors 被本轮 stage_finalize 重复提取）。
        if key_present(&state, "_plugin_errors") {
            set_key(&mut state, "_plugin_errors", serde_json::json!([]));
        }
        // run 边界窗口：起始基线写入进轨迹（凡 state 变更必可追溯）；失败经
        // 统一落库核重试，耗尽即上抛使 run 失败（轨迹缺失段不可对账重建）。
        self.append_window_trace("run_init", &state).await?;

        // ADR ②③：引擎独占落库。run 开始时建 runs 记录 + 落 user 消息。
        // 失败只 warn 不阻断执行（持久化不应让管道跑不通）。
        self.persist_run_start(&mut state, &compiled.config_hash)
            .await;

        // 观察路径：run 开始广播 OnPipelineStart（审计/`lifecycle.pipeline_start_total`）。
        self.emit_pipeline_lifecycle(agentos_core::traits::LifecycleHook::OnPipelineStart, &state)
            .await;

        // ── 多循环体执行 ──
        // 转移死循环防护：Phase 跳转/循环体数上限的乘积保险。
        let max_guard = compiled.bodies.len().saturating_mul(4).max(16);
        // 轮数硬上限计数（per-run：全部循环体的 while 轮累计）。
        let mut rounds_used: usize = 0;
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
            self.journal_top_key(&state, "current_phase");
            set_key(&mut state, "current_phase", serde_json::json!(body.id));
            // 体入口窗口：current_phase 切换进轨迹（凡 state 变更必可追溯）。
            self.append_window_trace("body_enter", &state).await?;
            // 收尾语义：管道已 ended 时，run_on_error 循环体仍照常执行（忽略 ended）
            let ignore_ended = truthy_flag(&state, "ended") && body.run_on_error;
            let iterations = self
                .execute_body(body, &mut state, compiled, ignore_ended, &mut rounds_used)
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
                // 消费一次性的 next_phase（不残留到 checkpoint/state）；消费删除
                // 亦是变更，journal 定格旧值供 body_route 窗口记 null 标记。
                self.journal_top_key(&state, "next_phase");
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
                if let Some(matched) = self.apply_routes(&body.exit_routes, &mut state) {
                    // apply_routes 的 Phase 分支会写 state.next_phase；此处已消费
                    // 返回值完成转移，立即移除，防止残留导致下一循环体结束时复跳。
                    // 消费删除同样 journal 定格（body_route 窗口记 null 标记）。
                    self.journal_top_key(&state, "next_phase");
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
            // 体间转移窗口：exit_routes 写入（apply_routes 内部首触实录）、
            // next_phase 消费删除、execute_body 轮边界的 should_stop→ended 折算
            // 统一在此落轨迹（此前体间变更不落任何轨迹）。
            self.append_window_trace("body_route", &state).await?;
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
        // 实际状态内核持有（2026-09-03 双状态裁定）：预期层控制键
        // （suspended/ended/router.stop_reason）经终态映射单点折算成 run_status
        // 写回 state——checkpoint / registry 快照 / runs 表三处同词汇同源，
        // 可观测性只读这一个字段。
        let terminal = agentos_core::types::RunStatus::from_control_state(&state);
        self.journal_top_key(&state, "run_status");
        if let Some(obj) = state.as_object_mut() {
            obj.insert("run_status".into(), serde_json::json!(terminal));
        }
        // run 收尾窗口：终态折算写入进轨迹；失败经统一落库核重试，耗尽即上抛
        // （与 run_init 同一严格口径，不允许 warn+计数软降级）。
        self.append_window_trace("run_finalize", &state).await?;
        self.persist_run_end(&state).await;

        // 观察路径：run 结束广播 OnPipelineEnd（审计/`lifecycle.pipeline_end_total`）。
        // 与点对点 end 钩子同边界——引擎 Err 提前返回的 run 不发（api 层 run.failed
        // 域事件承载该终态观测）。
        self.emit_pipeline_lifecycle(agentos_core::traits::LifecycleHook::OnPipelineEnd, &state)
            .await;

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
    /// Err = step 级跳转护栏触发（见 [`Self::execute_steps`]）或轮数硬上限超限，
    /// 向上传播终止 run。
    ///
    /// `rounds_used`：per-run 已执行 while 轮累计（跨循环体共享），达
    /// `max_rounds` 时 fail-closed 报错——while 条件恒真且终止检查插件持续
    /// 失败（插件错误统一 warn+continue，不终止循环）时的引擎层兜底。
    async fn execute_body(
        &self,
        body: &CompiledBody,
        state: &mut serde_json::Value,
        compiled: &CompiledPipeline,
        ignore_ended: bool,
        rounds_used: &mut usize,
    ) -> Result<i32, EngineError> {
        let mut iteration: i32 = 0;
        // 打开轮（LLM 回合）生命周期：轮次 = 一次 LLM 调用 + 其后紧跟的工具迭代
        // 链（同一回合）。DSL 契约：post 路由写 core_type 语义标志决定下一迭代
        // 形态（llm_call ↔ tool_execute 交替；core_plugin 只承载动态步骤的插件
        // 选择，由管道配置声明，引擎零插件 id 知识）——工具迭代不产生
        // assistant 消息，沿用打开轮的 message_id（tool 工具事件按 state
        // 的当前轮 id 寻址，与 LLM 轮 new_message 的 toolCalls 同卡位），
        // 不开新轮、不发 stream_start。若按迭代开轮，工具卡会被建到独立的
        // 工具轮占位消息上，与 LLM 轮的卡片重复（用户反馈的「尾部整段重复
        // 工具卡」根因，2026-08-27）。
        let mut open_round: Option<(i64, String, crate::round_events::RoundStart)> = None;
        // G10 单轨：循环模式 = while_cond 存在（编译期已归一）。正常终止信号
        // 是插件写的 state 标志（stop_check/ended）；插件错误统一 warn+continue
        // 不终止循环——引擎层另设轮数硬上限（max_rounds）兜底，防 while 恒真
        // 时 CPU/LLM 费用无限燃烧。
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
                // 轮数硬上限（fail-closed）：超限报错终止 run，非静默 break。
                if *rounds_used >= self.max_rounds {
                    return Err(EngineError::Other {
                        message: format!(
                            "轮数超限：run 已执行 {} 轮达上限 {}（max_rounds），循环体 '{}' 的 while 条件疑似恒真；按 fail-closed 终止 run",
                            rounds_used, self.max_rounds, body.id
                        ),
                    });
                }
                *rounds_used += 1;
                iteration += 1;
                // checkpoint 计数在 persist_step_trace 里按「配置 step」推进
                // （每执行一个配置 step +1，达 interval_steps 落档），此处不再按轮计数。
                let assistant_before = count_role(state, "assistant");
                // 先判迭代形态（post 路由写入的语义契约核心契约），再决定开轮/续轮：
                // 工具迭代沿用打开轮（tool 事件挂 LLM 轮消息）；LLM/其它迭代
                // 一律新开轮——先续后判会让下一次 LLM 迭代误沿上一轮的 id
                // （工具卡重复建到 LLM 轮消息的根因，2026-08-27 真机复现）。
                // 形态标志 = core_type（llm_call | tool_execute）：管道配置
                // post 路由按 DSL 写它决定下一迭代形态，换 core 插件实现
                // （自定义插件 id）轮次语义不变——引擎零插件 id 知识。
                let next_is_tool = state_str(state, "core_type").as_deref() == Some("tool_execute");
                let (round_index, round_id, round_start) = if next_is_tool {
                    if let Some((ri, rid, rs)) = open_round.take() {
                        if let Some(obj) = state.as_object_mut() {
                            obj.insert("message_id".into(), serde_json::json!(rid));
                        }
                        (ri, rid, rs)
                    } else {
                        // 异常形态（无打开轮却先跑工具迭代）：兜底开一轮（记录完整
                        // 性优先；正常 DSL 流不会走到此处）。
                        self.round_start_event(state).await
                    }
                } else {
                    self.round_start_event(state).await
                };
                self.execute_steps(&body.steps, &body.id, state, compiled, ignore_ended)
                    .await?;
                if count_role(state, "assistant") > assistant_before {
                    // 本轮（LLM 回合）产出 assistant → 发终态事件（new_message/stream_end）。
                    // 回合保持「打开」直到下一个 LLM 迭代（随后的工具迭代仍沿用本轮
                    // message_id——工具事件挂本轮消息，与 new_message.toolCalls 同卡位）。
                    self.round_end_event(
                        state,
                        round_index,
                        round_id.clone(),
                        &round_start,
                        assistant_before,
                    )
                    .await;
                }
                open_round = Some((round_index, round_id, round_start));
                // 控制状态键契约（ADR 2026-08-30）：插件只写 state——should_stop
                // 在轮边界折算为 ended，复用既有收尾语义（run_on_error 收尾体
                // 照跑）；终止原因由写方随 router.stop_reason 署名，run 收尾
                // 按署名映射终态。
                if truthy_flag(state, "should_stop") && !truthy_flag(state, "ended") {
                    self.journal_top_key(state, "ended");
                    set_key(state, "ended", serde_json::Value::Bool(true));
                }
                if truthy_flag(state, "suspended") {
                    break;
                }
                if !ignore_ended && truthy_flag(state, "ended") {
                    break;
                }
            }
        } else {
            // 单次执行（前处理/后处理体）：同样构成一轮（消息事件语义与循环迭代一致）
            let assistant_before = count_role(state, "assistant");
            let (round_index, round_id, round_start) = self.round_start_event(state).await;
            self.execute_steps(&body.steps, &body.id, state, compiled, ignore_ended)
                .await?;
            self.round_end_event(state, round_index, round_id, &round_start, assistant_before)
                .await;
        }
        Ok(iteration)
    }

    /// 轮次开始边界（DSH 形态：一次 body 执行 = 一条消息）：分配本轮消息 id 并注入
    /// state。llm_core 经 _call_context 携带、tool_core 直接读——本条消息的全部
    /// 流式/工具事件按本轮 id 精确寻址；`_assistant_id_assigned` 每轮复位，使本轮
    /// 首个 assistant 追加 op 携带本轮 id 作 record_id（流式占位与 DB 重载逐轮同构）。
    async fn round_start_event(
        &self,
        state: &mut serde_json::Value,
    ) -> (i64, String, crate::round_events::RoundStart) {
        let round_index = self.round_counter.fetch_add(1, Ordering::Relaxed) + 1;
        let round_id = format!("a_{}", uuid::Uuid::new_v4().simple());
        if let Some(obj) = state.as_object_mut() {
            obj.insert("message_id".into(), serde_json::json!(round_id));
            obj.insert("_assistant_id_assigned".into(), serde_json::json!(false));
        }
        let start = crate::round_events::RoundStart {
            round_index,
            message_id: round_id.clone(),
            pipeline_id: state_str(state, "pipeline_id").unwrap_or_default(),
            thread_id: state_str(state, "session_id")
                .or_else(|| state_str(state, "thread_id"))
                .unwrap_or_default(),
        };
        if let Some(ev) = &self.round_events {
            ev.on_round_start(start.clone()).await;
        }
        (round_index, round_id, start)
    }

    /// 轮次结束边界：本轮新增了 assistant 才携带其完整持久形态（None = 本轮
    /// 无产出，消费方只发 stream_end 收尾，不发 new_message）。user 消息在
    /// 每轮都携带——消费方（api 桥接）只在首个有产出的轮次附认领回传。
    async fn round_end_event(
        &self,
        state: &serde_json::Value,
        round_index: i64,
        round_id: String,
        start: &crate::round_events::RoundStart,
        assistant_before: usize,
    ) {
        if let Some(ev) = &self.round_events {
            let assistant = if count_role(state, "assistant") > assistant_before {
                last_role(state, "assistant")
            } else {
                None
            };
            ev.on_round_end(crate::round_events::RoundEnd {
                round_index,
                message_id: round_id,
                pipeline_id: start.pipeline_id.clone(),
                thread_id: start.thread_id.clone(),
                assistant,
                user_message: last_role(state, "user"),
            })
            .await;
        }
    }

    /// 向生命周期事件总线广播管道级事件（观察路径，best-effort、非阻塞）。
    ///
    /// 目标 = [`EventTarget::Pipeline`]（pipeline_id），上下文带 pipeline_id/run_id
    /// 标签（与点对点 end 钩子的 HookContext 同源）。未注入总线时零操作。
    async fn emit_pipeline_lifecycle(
        &self,
        hook: agentos_core::traits::LifecycleHook,
        state: &serde_json::Value,
    ) {
        let Some(bus) = self.hook_bus.as_ref() else {
            return;
        };
        let pipeline_id = state
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let mut ctx = agentos_core::traits::HookContext::new();
        ctx.set("pipeline_id", serde_json::json!(pipeline_id));
        ctx.set("run_id", serde_json::json!(self.run_id));
        bus.emit(LifecycleEvent {
            hook,
            ctx,
            target: EventTarget::Pipeline(pipeline_id),
            ts: SystemTime::now(),
        });
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
        body_id: &str,
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
            let routed = self
                .execute_step(step, body_id, state, compiled, ignore_ended)
                .await?;
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
    /// `body_id`：宿主循环体 id——step 级 hooks 的复合作用域键 `"<body>:<step>"`
    /// 分发定位用（服务化提案 §3.6 声明位置即作用域）。
    ///
    /// 由于 `execute_step` 与 `execute_step_inner` 相互递归调用（Composite 项会
    /// 递归执行），Rust async fn 无法直接表达无限大小的 future，
    /// 这里用 `Box::pin` 引入间接层（boxed future）打破无限大小。
    fn execute_step<'a>(
        &'a self,
        step: &'a CompiledStep,
        body_id: &'a str,
        state: &'a mut serde_json::Value,
        compiled: &'a CompiledPipeline,
        ignore_ended: bool,
    ) -> std::pin::Pin<
        Box<dyn std::future::Future<Output = Result<Option<RouteNext>, EngineError>> + Send + 'a>,
    > {
        Box::pin(self.execute_step_impl(step, body_id, state, compiled, ignore_ended))
    }

    async fn execute_step_impl(
        &self,
        step: &CompiledStep,
        body_id: &str,
        state: &mut serde_json::Value,
        compiled: &CompiledPipeline,
        ignore_ended: bool,
    ) -> Result<Option<RouteNext>, EngineError> {
        // step 边界清空实录缓冲（messages ops 与顶层键 journal 同点位）。
        // 轨迹颗粒度 = 配置 step（prepare/core/post 等），不钻插件。
        // patch_data 含本 step 期间所有顶层 key 变更；messages 走 ops 实录（见 ops_ledger）。
        self.ops_ledger.lock().clear();
        self.step_key_journal.lock().clear();

        // B 区登记挂点（ADR 2026-08-27 §2.2）：step 进入时登记 message→step
        // 归属（流式窗口 = 本 step invoke 期间），守卫在 step 收尾自动清除。
        let _binding = self.bind_step_message(state, &step.id);

        // hooks 同步边界分发（服务化提案 §3.6 同步边界事件）：step 进入时
        // 分发 "step_start"（step 级 + body 级两档作用域查表直派，fire-and-forget）。
        self.dispatch_boundary_hooks(
            compiled,
            state,
            &HookScope::Step(format!("{body_id}:{}", step.id)),
            "step_start",
        )
        .await;

        // 1. context 注入：渲染 step.context 模板（模板原文保留，动态点），merge 进 state
        let rendered = render_value(
            &serde_json::to_value(&step.context)
                .unwrap_or(serde_json::Value::Object(Default::default())),
            state,
            &self.project_root,
        );
        if let Some(obj) = rendered.as_object() {
            for (k, v) in obj {
                self.journal_top_key(state, k);
                set_key(state, k, v.clone());
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
                    self.execute_step_inner(step, body_id, state, compiled, ignore_ended)
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
                // hooks 同步边界分发（收尾）：step_end 两档作用域（与下方非循环
                // 分支同一收尾路径，循环 step 不缺席）
                self.dispatch_boundary_hooks(
                    compiled,
                    state,
                    &HookScope::Step(format!("{body_id}:{}", step.id)),
                    "step_end",
                )
                .await;
                return Ok(None);
            }
        }

        // 3. 非循环：直接执行
        self.execute_step_inner(step, body_id, state, compiled, ignore_ended)
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

        // hooks 同步边界分发（收尾）：step 结束时分发 "step_end"；钩子返回
        // `{"decision":"terminate"}` → 置 ended=true（与引擎既有 ended 语义
        // 对齐：当前循环体立即终止，后续循环体/收尾体照常推进）。
        self.dispatch_boundary_hooks(
            compiled,
            state,
            &HookScope::Step(format!("{body_id}:{}", step.id)),
            "step_end",
        )
        .await;
        Ok(routed)
    }

    /// hooks 同步边界分发（服务化提案 §3.6 同步边界事件）。
    ///
    /// 声明位置即作用域：step 边界事件（`step_start`/`step_end`）同时查
    /// step 级（复合键 `"<body>:<step>"`）与 body 级（`Body(body_id)`）两档
    /// 装载表，命中即 fire-and-forget 分发——经 invoker 对目标插件发起
    /// execute 调用，config 注入约定字段 `_pipe_hook = {"event", "payload"}`，
    /// payload 含 step_id/event/时间戳最小上下文。
    ///
    /// 返回值收集：任一钩子返回 `{"decision": "terminate"}` → 置 `ended=true`
    /// （与引擎既有 ended 语义对齐：当前循环体立即终止，后续循环体/收尾体
    /// 照常推进——对齐 transient-register ADR §二.7 结构化否决指令模型，钩子
    /// 只决策不路由）+ `tracing::warn` 留痕。分发失败仅 warn 不阻断（
    /// fire-and-forget 语义：决策窗扩展点不得让主流程翻车）。
    ///
    /// 空表短路：无任何命中（step_hooks 空或作用域不匹配）直接返回——
    /// 无钩子配置的主干零开销（§3.6 空集短路）。
    ///
    /// 现状：生产编译产物 step_hooks 恒空（加载入口未接 hooks，ADR
    /// 2026-09-09-kernel-dead-layer-adjudication）——本分发点当前运行时不命中，
    /// 接线后自动生效。
    async fn dispatch_boundary_hooks(
        &self,
        compiled: &CompiledPipeline,
        state: &mut serde_json::Value,
        step_scope: &HookScope,
        event: &str,
    ) {
        let mut hit = compiled.hooks_for(step_scope, event);
        if let HookScope::Step(step_key) = step_scope {
            // body 级作用域：step 执行期间的同步边界事件同发 body 级钩子
            // （body_id = 复合键 `"<body>:<step>"` 的前段）
            if let Some((body_id, _)) = step_key.split_once(':') {
                hit.extend(compiled.hooks_for(&HookScope::Body(body_id.to_string()), event));
            }
        }
        if hit.is_empty() {
            return; // 空集短路：无钩子配置的主干零开销
        }
        // 收集 step_id/pipeline_id 最小上下文（payload 字段）
        let step_id = match step_scope {
            HookScope::Step(key) => key.clone(),
            HookScope::Body(id) => id.clone(),
        };
        let pipeline_id = state_str(state, "pipeline_id");
        let content_loader = ContentLoader::new(
            Arc::clone(&self.store),
            self.run_id.clone(),
            self.branch_id.clone(),
        );
        let mut terminated = false;
        for entry in hit {
            let payload = serde_json::json!({
                "step_id": step_id,
                "event": event,
                "timestamp": chrono::Utc::now().to_rfc3339(),
                "pipeline_id": pipeline_id,
            });
            let config =
                serde_json::json!({ "_pipe_hook": { "event": event, "payload": payload } });
            let hook_ctx = PluginContext::new(
                &*state,
                config,
                self.default_tenant.clone(),
                uuid::Uuid::nil(),
                content_loader.clone(),
            );
            match self
                .invoker
                .invoke_pipeline_plugin(&entry.plugin_id, &hook_ctx)
                .await
            {
                Ok(result) => {
                    if result.error.is_none() {
                        // 结构化否决指令（transient-register ADR §二.7）：钩子只
                        // 决策不路由——返回 `{"decision":"terminate", ...}` →
                        // 引擎置 ended=true（SDK 契约：state_updates 平铺键）
                        if result
                            .state_updates
                            .get("decision")
                            .and_then(|v| v.as_str())
                            == Some("terminate")
                        {
                            terminated = true;
                        }
                    } else {
                        warn!(
                            plugin = %entry.plugin_id,
                            event = %event,
                            error = ?result.error,
                            "hook dispatch plugin error (fire-and-forget, continue)"
                        );
                    }
                }
                Err(e) => {
                    warn!(
                        plugin = %entry.plugin_id,
                        event = %event,
                        error = %e,
                        "hook dispatch failed (fire-and-forget, continue)"
                    );
                }
            }
        }
        if terminated {
            warn!(event = %event, step = %step_id, "hook decision terminate——置 ended=true 终止当前循环体");
            self.journal_top_key(state, "ended");
            set_key(state, "ended", serde_json::Value::Bool(true));
        }
    }

    /// 落 step 级轨迹：checkpoint 计步 + 窗口轨迹落库 + 分层投影的 step 入口。
    /// diff/实录/字段过滤语义见 [`Self::append_window_trace`]。
    ///
    /// **每执行一个配置 step 必调本函数**（组级 when 跳过的 step 在 execute_steps
    /// 直接 continue，不进本函数）——checkpoint 按步计数即在此推进
    /// （[`Self::count_step_and_maybe_checkpoint`]），保证"实际执行的 step"才计步。
    /// 顶层键首触实录（写前调用）：step 内首次触碰某键时定格其旧值，
    /// 供 [`Self::append_window_trace`] 算 diff——内核内 state 借用不克隆，
    /// 实录体积 ∝ 实际变更面而非 state 体积。
    fn journal_top_key(&self, state: &serde_json::Value, key: &str) {
        let mut journal = self.step_key_journal.lock();
        journal
            .entry(key.to_string())
            .or_insert_with(|| state.get(key).cloned());
    }

    /// 维护平铺键 `track.messages_chars`（W2a 上下文估算账本）：messages ops
    /// 应用后按 delta 增量推进；键缺失/为 0 且 messages 非空时全量重算一次
    /// （冷启动兜底——恢复/旧会话无账可依时宁可重算不可漏账）。
    ///
    /// 单位与一致性基准见 [`message_content_bytes`]（紧凑 JSON UTF-8 字节，
    /// 剔除 seq）。写入走 journal + set_key（进 step 轨迹，检查点/回放同源）。
    fn maintain_messages_chars(&self, state: &mut serde_json::Value, delta: i64) {
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
    async fn retry_persist<T, F, Fut>(
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

    async fn persist_step_trace(
        &self,
        step_id: &str,
        ckpt: &agentos_core::types::CheckpointConfig,
        state_after: &serde_json::Value,
    ) -> Result<(), EngineError> {
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
    async fn append_window_trace(
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
            run_id: self.run_id.clone(),
            branch_id: self.branch_id.clone(),
            seq_in_branch: 0,
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
    async fn project_state_snapshot(&self, state: &serde_json::Value) -> Result<(), EngineError> {
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

    /// 执行已编译列表项（G10：引用已在加载期解析为三类——插件 / composite / 动态模板）。
    ///
    /// 运行时只做：项级 when AST 求值（零解析）→ 按类别分派。composite 查统一步骤池
    /// 递归执行；动态模板项渲染后走同样的池/插件查找（显式保留的动态点）。
    /// `body_id` 透传给递归 composite（hook 复合作用域键 `"<body>:<step>"` 定位）。
    async fn execute_step_inner(
        &self,
        step: &CompiledStep,
        body_id: &str,
        state: &mut serde_json::Value,
        compiled: &CompiledPipeline,
        ignore_ended: bool,
    ) -> Result<(), EngineError> {
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
                    self.execute_step(&target, body_id, state, compiled, ignore_ended)
                        .await?;
                }
                // 静态命中插件（per-plugin inputs 经 config 通道传给插件，
                // 不 merge 进 state、不落 trace；具名步骤服务经 _step_method
                // 约定字段透传，SDK 侧按名分发）
                CompiledItem::Plugin {
                    plugin_id,
                    method,
                    inputs,
                    ..
                } => {
                    if self
                        .invoke_item_plugin(plugin_id, method.as_deref(), inputs, state)
                        .await
                    {
                        break; // skip_remaining
                    }
                }
                // 动态点：模板名（{{state.xxx}}），渲染后走池 → 插件
                CompiledItem::Dynamic { template, .. } => {
                    let resolved = render_template(template, state, &self.project_root);
                    if let Some(target) = compiled.find_step(&resolved) {
                        let target = target.clone();
                        self.execute_step(&target, body_id, state, compiled, ignore_ended)
                            .await?;
                    } else if self.lookup_plugin(&resolved) {
                        // 动态点无静态 inputs/method（模板运行时才定），传空
                        if self
                            .invoke_item_plugin(&resolved, None, &HashMap::new(), state)
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
        Ok(())
    }

    /// 调用原子插件并 merge state_updates；返回 true = 应跳过同组后续
    /// （`skip_remaining`）。插件错误统一 warn + 继续（不再按 error_policy 分发，ADR 2026-08-18）。
    ///
    /// `inputs`：per-plugin 输入（经 config 通道传给插件，不进 state、不落 trace；
    /// 空 = 等价旧行为）。
    ///
    /// `method`：具名步骤服务（服务化提案 §3.4 传输通道）——`Some(步骤名)` 时
    /// config 携带约定字段 `_step_method`（invoker::shared::with_step_method），
    /// SDK 侧分发到对应注册函数；`None` = 默认 execute 入口（现状零改动）。
    ///
    /// 错误可见性：插件失败（result.error / invoker Err）除 warn 外，追加到
    /// state 的 `_plugin_errors` 键（`[{plugin_id, code, message}]` 数组）——
    /// run 结束由 api 层提取为 EngineOutcome.plugin_errors，WS 路径发射
    /// plugin_error 事件弹前端通知（引擎 warn+继续的假成功不再完全静默）。
    /// 下划线前缀键不参与插件 state_updates 合并（merge 只写插件声明的键）。
    async fn invoke_item_plugin(
        &self,
        plugin_id: &str,
        method: Option<&str>,
        inputs: &HashMap<String, serde_json::Value>,
        state: &mut serde_json::Value,
    ) -> bool {
        match self.invoke_plugin(plugin_id, method, inputs, &*state).await {
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
                    record_plugin_error(state, plugin_id, result.error.as_ref().unwrap());
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
                record_plugin_error(state, plugin_id, &e);
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
    ///
    /// `method`：具名步骤服务时 config 注入约定字段 `_step_method`（与 inputs
    /// 同对象共存，SDK 侧按名分发；None = 默认 execute 入口零改动）。
    ///
    /// `state` 为借用：invoke 期间引擎不写 state（结果在返回后 merge），
    /// 消除每步整包深拷。
    async fn invoke_plugin(
        &self,
        plugin_id: &str,
        method: Option<&str>,
        inputs: &HashMap<String, serde_json::Value>,
        state: &serde_json::Value,
    ) -> Result<PluginResult, PluginError> {
        // 监控 M2：step 命中（每 invoke 一次 = 命中一个 step 的插件）
        self.metrics.inc_step_hit();
        let content_loader = ContentLoader::new(
            Arc::clone(&self.store),
            self.run_id.clone(),
            self.branch_id.clone(),
        );
        let mut config = if inputs.is_empty() {
            serde_json::Value::Object(Default::default())
        } else {
            serde_json::json!({ "inputs": inputs })
        };
        if let Some(name) = method {
            config = agentos_invoker::shared::with_step_method(config, name);
        }
        let ctx = PluginContext::new(
            state,
            config,
            self.default_tenant.clone(),
            uuid::Uuid::nil(),
            content_loader,
        );
        // 监控 M2：LLM/工具调用次数 + 耗时（调度层视角 = invoke 前后差，
        // 监控设计 §补引擎调度层）。类别按显式映射表登记
        // （plugin_metrics_class），未收录 id = Other 不计（编排类插件）。
        let class = plugin_metrics_class(plugin_id);
        let invoke_start = std::time::Instant::now();
        let result = self.invoker.invoke_pipeline_plugin(plugin_id, &ctx).await;
        let elapsed = invoke_start.elapsed().as_micros() as u64;
        match class {
            PluginMetricsClass::Llm => self.metrics.inc_llm_call(elapsed),
            PluginMetricsClass::Tool => self.metrics.inc_tool_call(elapsed),
            PluginMetricsClass::Other => {}
        }
        result
    }

    // ── 持久化（ADR ②③：引擎独占落库，插件只返回 Patch）──────────

    /// 创建运行实例并登记 run 坐标：runs 表建行、run→pipeline 归属、
    /// pipeline↔session 映射，以及首轮 user_input 轨迹。
    ///
    /// 在 run() 开头调用一次。用户消息全文由派发层在 run 前经 message_slots
    /// append op 落库（引擎不单独写 messages 表）；state 各键由
    /// server.rs 的 stage_build_initial_state 注入。
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
            if let Err(e) = self
                .store
                .set_run_pipeline(&self.run_id, run_pipeline_id)
                .await
            {
                warn!(
                    run_id = %self.run_id,
                    pipeline_id = %run_pipeline_id,
                    error = %e,
                    "set_run_pipeline 归属登记失败（继续执行）"
                );
                self.metrics.inc_persist_failure();
            }
        }

        // pipeline_id 从 state 读（stage_build_initial_state 注入，前端创建会话
        // 时生成、每轮回传）。它是消息层查询主键，适配"通过 state 通路执行持久化"。
        let pipeline_id = state
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");

        // 写入 pipeline↔session 映射（每次管道开跑时记录，含子任务管道）。
        // 删除会话时据此按 thread_id 找到全部 pipeline_id 级联清理。
        // session_id 即 thread_id（stage_build_initial_state 注入）；两者任一为空则跳过（幂等）。
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

        // update_run_status 要求 current_branch 和 current_seq 同时 Some 或同时 None。
        // 终态映射单点（控制状态键契约 ADR 2026-08-30）：RunStatus::from_control_state
        // 与域事件派生（api derive_run_terminal_events）共用，两处不得各持一套词汇。
        let final_status = agentos_core::types::RunStatus::from_control_state(final_state);
        if let Err(e) = self
            .retry_persist("update_run_status", || {
                let status = final_status.clone();
                async move {
                    self.store
                        .update_run_status(&self.run_id, status, None, None)
                        .await
                }
            })
            .await
        {
            // B5：终态落库重试耗尽——run 行滞留 running，G8 优雅重启排空会把
            // 已结束的 run 误标 suspended（当可恢复挂起）、崩溃清扫会误标 failed。
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
    async fn write_terminal_persist_failed_marker(
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

    /// merge 插件 state_updates 进 state（纯内存合并）。
    ///
    /// messages **只接受 op 声明**（`{_ops:[set/insert]}`）→ "一次 apply" 到内存、
    /// 表、实录、字符记账四落点。全量数组形式零兼容：收到即 warn 丢弃——改队列
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
                    // W2a 记账：old 侧字节须读应用前数组——先算 delta 再 apply。
                    let chars_delta = messages_ops_chars_delta(state, &ops_owned);
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
                            // 落库自动清（ADR 2026-08-27 §2.2 生命周期第二清）：
                            // 消息最终形态已落 message_slots，同 message_id 的
                            // chunk 中间态 + B 区绑定使命完成。
                            self.clear_transient_for_ops(state, &ops_owned, &tenant_id);
                        }
                        Err(e) => {
                            warn!(error = %e, "apply_messages_op_update 失败（继续）");
                            self.metrics.inc_persist_failure();
                        }
                    }
                    // 记账对齐**内存**数组：apply 内内存先变、表侧失败才返 Err，
                    // Ok/Err 均推进（表侧丢失语义与 ops 实录缺席同款降级，见上方 warn）。
                    self.maintain_messages_chars(state, chars_delta);
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
            self.journal_top_key(state, k);
            set_key(state, k, v.clone());
        }
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
    fn clear_transient_for_ops(
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
    fn bind_step_message(
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
    fn apply_routes(
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
                    self.journal_top_key(state, k);
                    set_key(state, k, v.clone());
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
pub struct StepBindingGuard {
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

/// 追加一条插件错误到 state 的 `_plugin_errors` 数组（引擎内部键，插件
/// state_updates 不会写它；api 层 run 结束后提取为 EngineOutcome.plugin_errors）。
fn record_plugin_error(state: &mut serde_json::Value, plugin_id: &str, err: &PluginError) {
    let entry = serde_json::json!({
        "plugin_id": plugin_id,
        "code": err.code.clone().unwrap_or_else(|| "PLUGIN_EXEC_FAILED".to_string()),
        "message": err.message,
    });
    let arr = state
        .get_mut("_plugin_errors")
        .and_then(|v| v.as_array_mut());
    match arr {
        Some(arr) => arr.push(entry),
        None => {
            set_key(state, "_plugin_errors", serde_json::json!([entry]));
        }
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

/// 读取 state[key] 的字符串（缺失/非 string 返回 None）。
fn state_str(state: &serde_json::Value, key: &str) -> Option<String> {
    state
        .as_object()
        .and_then(|o| o.get(key))
        .and_then(|v| v.as_str())
        .map(str::to_string)
}

/// state["messages"] 中指定 role 的消息数。
fn count_role(state: &serde_json::Value, role: &str) -> usize {
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
fn last_role(state: &serde_json::Value, role: &str) -> Option<serde_json::Value> {
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

/// 计算 step 执行前后的 state diff：返回 after 中相对 before 变更的顶层 key 及其新值。
/// - 新增的 key：纳入 diff
/// - 值变化的 key：纳入 diff（after 的值）
/// - 值相同的 key：跳过
/// - 键被移除：记 null（RFC 7396 Merge-Patch 语义，回放端 merge_patch 以 null
///   执行删除）——审计账本要求删除可追溯；首触即不存在的（step 内净增净删）
///   净无变化，不记
///
/// 非顶层（深层）变更按整体替换（不递归细粒度 diff），对齐 step 级快照语义。
/// messages **不参与 diff**——它走 ops 实录（ops_ledger，插件声明、指纹降级），
/// 全量数组 diff 推断不适用（零兼容）。
/// step 顶层键首触实录：记录写前旧值（首触即定格——后续同键再写不覆盖，
/// diff 只关心 step 边界前后的变化）。调用方在写 state 前调用。
fn state_diff_from_journal(
    journal: &HashMap<String, Option<serde_json::Value>>,
    after: &serde_json::Value,
) -> serde_json::Value {
    let after_obj = match after.as_object() {
        Some(o) => o,
        None => return serde_json::Value::Object(Default::default()),
    };
    let mut diff = serde_json::Map::new();
    for (k, old) in journal {
        if k == "messages" {
            continue; // 轨迹由 ops_ledger 实录提供，diff 不推断
        }
        if k == "_plugin_errors" {
            continue; // 引擎内部键（插件错误收集），不进 trace——回放重建时由 run 开头清空重建
        }
        let Some(v_after) = after_obj.get(k) else {
            // 键被移除：首触已有旧值 → 记 null（merge_patch 语义的删除标记）；
            // 首触即不存在 → step 内净无此键，无可记变化。
            if old.is_some() {
                diff.insert(k.clone(), serde_json::Value::Null);
            }
            continue;
        };
        let changed = match old {
            Some(v_old) => v_old != v_after,
            None => true, // step 内新增 key
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

// ── track.messages_chars 记账（W2a 上下文估算账本）─────────────────
//
// `track.messages_chars` = state["messages"] 全体消息的紧凑 JSON UTF-8 字节
// 总和，是管道 YAML 压缩粗门公式的字符账本。引擎在此增量维护（每 op O(1)，
// insert 顺延为 O(n) 但现网零使用），领域公式写在管道 when 里——引擎零领域知识。

/// 单条消息的度量单位：**剔除 seq 后**的紧凑 JSON UTF-8 字节数
/// （`serde_json::to_string(msg).len()`）。
///
/// seq 是引擎分配的槽位元数据而非消息内容：insert op 顺延后段 seq、append
/// 分配新 seq 都不改内容字节——剔除后增量记账与全量重算（[`messages_chars_total`]）
/// 严格一致，两侧共用本函数为唯一定义。
fn message_content_bytes(msg: &serde_json::Value) -> i64 {
    let stripped;
    let m = match msg {
        serde_json::Value::Object(o) if o.contains_key("seq") => {
            let mut c = o.clone();
            c.remove("seq");
            stripped = serde_json::Value::Object(c);
            &stripped
        }
        _ => msg,
    };
    serde_json::to_string(m)
        .map(|s| s.len() as i64)
        .unwrap_or(0)
}

/// 全量重算：messages 全体消息的字节总和（冷启动兜底值 + 一致性性质基准）。
fn messages_chars_total(state: &serde_json::Value) -> i64 {
    state
        .get("messages")
        .and_then(|v| v.as_array())
        .map(|a| a.iter().map(message_content_bytes).sum())
        .unwrap_or(0)
}

/// 计算一组 messages ops 对字节总和的增量。**必须在 ops 应用前调用**
/// （old 侧读应用前数组）；同批多 op 按序回放（后序 op 的旧值含前序 op 的
/// 效果），与 `apply_messages_op_update` 对同一批的顺序应用一致。
///
/// 增量规则（单位见 [`message_content_bytes`]，seq 剔除口径下 insert 顺延零成本）：
/// - `set` + msg 对象：seq 处有旧消息 → `new - old`；无旧消息（含引擎分配
///   seq 的 append）→ `+new`
/// - `set` + msg=null/缺省（delete）：`-old`（无旧消息为 no-op）
/// - `insert`：`+new`
fn messages_ops_chars_delta(state: &serde_json::Value, ops: &[serde_json::Value]) -> i64 {
    // 旧值视角 = seq → 内容字节
    let mut seq_bytes: HashMap<i64, i64> = HashMap::new();
    if let Some(arr) = state.get("messages").and_then(|v| v.as_array()) {
        for m in arr {
            if let Some(seq) = m.get("seq").and_then(|s| s.as_i64()) {
                seq_bytes.insert(seq, message_content_bytes(m));
            }
        }
    }
    // 与 apply_messages_op_update 同规则推进 max_seq（无 seq 的 set = append
    // 分配 max+1，显式 seq 推进 max）——同批视角一致。
    let mut max_seq = seq_bytes.keys().copied().max().unwrap_or(-1);
    let mut delta: i64 = 0;
    for op in ops {
        let kind = op.get("op").and_then(|v| v.as_str()).unwrap_or("");
        match kind {
            "set" => match op.get("seq").and_then(|v| v.as_i64()) {
                None => {
                    // append：引擎分配 max+1。resolve 的 max 不看 insert 的 at——
                    // 同批 insert 顺延可把元素挪进该槽位，此时 apply 实为**替换**
                    // （同 seq 替换内容），delta 须扣旧值；纯新槽位 old=0 自然退化为 +new。
                    max_seq += 1;
                    if let Some(msg) = op.get("msg").filter(|m| m.is_object()) {
                        let new = message_content_bytes(msg);
                        let old = seq_bytes.get(&max_seq).copied().unwrap_or(0);
                        delta += new - old;
                        seq_bytes.insert(max_seq, new);
                    }
                    // msg=null 的 append 在 apply 侧为 no-op（分配 seq 无元素可删）——不计
                }
                Some(seq) => {
                    if seq > max_seq {
                        max_seq = seq;
                    }
                    let old = seq_bytes.get(&seq).copied().unwrap_or(0);
                    match op.get("msg") {
                        Some(msg) if msg.is_object() => {
                            let new = message_content_bytes(msg);
                            delta += new - old;
                            seq_bytes.insert(seq, new);
                        }
                        _ => {
                            delta -= old;
                            seq_bytes.remove(&seq);
                        }
                    }
                }
            },
            "insert" => {
                let Some(at) = op.get("at").and_then(|v| v.as_i64()) else {
                    continue;
                };
                // 后段顺延：seq>=at 的键 +1（内容字节不变，仅键移动）
                let shifted: Vec<(i64, i64)> = seq_bytes
                    .iter()
                    .filter(|(s, _)| **s >= at)
                    .map(|(s, b)| (*s, *b))
                    .collect();
                for (s, _) in &shifted {
                    seq_bytes.remove(s);
                }
                for (s, b) in shifted {
                    seq_bytes.insert(s + 1, b);
                }
                if let Some(msg) = op.get("msg") {
                    let new = message_content_bytes(msg);
                    delta += new;
                    seq_bytes.insert(at, new);
                }
                // 注意：不推进 max_seq——apply 的 resolve 只按 seq 字段推进
                // （insert 的 at 不参与），同批视角必须与之一致。
            }
            _ => {}
        }
    }
    delta
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

/// 监控 M2 插件指标类别（词表，替代已废的 plugin_id 子串猜测）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PluginMetricsClass {
    /// LLM 调用：计入 `llm_calls_total` / `llm_calls_micros`。
    Llm,
    /// 工具调用：计入 `tool_calls_total` / `tool_calls_micros`。
    Tool,
    /// 其他编排/服务插件：不计入 LLM/tool 指标。
    Other,
}

/// 插件 id → 指标类别显式映射（监控分类词表，非行为特判——分类结果只进
/// metrics，不影响任何执行路径）。
///
/// 契约：
/// - 键 = plugin.json 的 `id` 精确匹配（不做大小写归一、不做子串猜测）；
/// - 覆盖现存承载 LLM/tool 调用语义的插件全集，未收录 id 一律 `Other`；
/// - 新插件若承载 LLM/tool 调用语义须在此登记，漏登 = 指标少计（可观测
///   债务），不构成行为故障；分类基线由
///   `test_plugin_metrics_class_locked_to_current_plugin_set` 锁定。
const METRICS_PLUGIN_CLASS: &[(&str, PluginMetricsClass)] = &[
    // LLM 调用面
    ("llm_service", PluginMetricsClass::Llm),
    ("pipeline_llm_core", PluginMetricsClass::Llm),
    // 工具调用面（管道工具核 + 工具插件）
    ("agentos-builtin-tools", PluginMetricsClass::Tool),
    ("bash_tool", PluginMetricsClass::Tool),
    ("download_tool", PluginMetricsClass::Tool),
    ("human_interaction_tool", PluginMetricsClass::Tool),
    ("media_tools", PluginMetricsClass::Tool),
    ("memory_tool", PluginMetricsClass::Tool),
    ("pipeline_tool_cache", PluginMetricsClass::Tool),
    ("pipeline_tool_cache_writer", PluginMetricsClass::Tool),
    ("pipeline_tool_core", PluginMetricsClass::Tool),
    ("pipeline_tool_schema", PluginMetricsClass::Tool),
    ("pipeline_tool_schema_validator", PluginMetricsClass::Tool),
    ("project_create_tool", PluginMetricsClass::Tool),
    ("resource_merge_tool", PluginMetricsClass::Tool),
    ("resource_search_tool", PluginMetricsClass::Tool),
    ("simple_tools", PluginMetricsClass::Tool),
    ("spill_retrieve_tool", PluginMetricsClass::Tool),
    ("task_evaluate_tool", PluginMetricsClass::Tool),
    ("task_manage_tool", PluginMetricsClass::Tool),
    ("task_submit_tool", PluginMetricsClass::Tool),
    ("trigger_setup_tool", PluginMetricsClass::Tool),
    ("web_operate_tool", PluginMetricsClass::Tool),
];

/// 按显式映射表分类插件 id（未收录 → `Other`）。
fn plugin_metrics_class(plugin_id: &str) -> PluginMetricsClass {
    METRICS_PLUGIN_CLASS
        .iter()
        .find(|(id, _)| *id == plugin_id)
        .map(|(_, class)| *class)
        .unwrap_or(PluginMetricsClass::Other)
}

// ═════════════════════════════════════════════════════════════════
// 单元测试
// ═════════════════════════════════════════════════════════════════

#[cfg(test)]
mod state_diff_journal_tests {
    use super::*;
    use serde_json::json;

    /// journal diff 语义边界三态：变更/新增入选；写回原值与未触碰键、被移除键、
    /// messages/_plugin_errors 一律不进 diff（与旧全量 diff 行为逐条对齐）。
    #[test]
    fn diff_from_journal_semantics() {
        let mut journal = HashMap::new();
        journal.insert("a".to_string(), Some(json!(1))); // 写回原值 → 排除
        journal.insert("b".to_string(), Some(json!(2))); // 变更 → 入选
        journal.insert("c".to_string(), None); // step 内新增 → 入选
        journal.insert("d".to_string(), Some(json!(4))); // 键被移除 → null 入选
        journal.insert("messages".to_string(), Some(json!([]))); // 排除键
        journal.insert("_plugin_errors".to_string(), None); // 排除键
        let after = json!({
            "a": 1,      // 未变
            "b": 3,      // 变更
            "c": 5,      // 新增
            "e": 9,      // 未触碰（不在 journal）→ 排除
        });
        let diff = state_diff_from_journal(&journal, &after);
        assert_eq!(diff, json!({"b": 3, "c": 5, "d": null}));
    }

    /// after 非 Object（防御路径）：diff 为空对象。
    #[test]
    fn diff_from_journal_non_object_after_is_empty() {
        let mut journal = HashMap::new();
        journal.insert("a".to_string(), Some(json!(1)));
        assert_eq!(state_diff_from_journal(&journal, &json!("str")), json!({}));
    }

    /// 键被移除的性质断言：diff 应用到 step 前状态须能重建 after 的键集——
    /// 「首触存在、after 缺失」产出 null 删除标记；「首触即不存在、after 亦无」
    /// 是 step 内净增净删，净无变化，不得产出任何标记。
    #[test]
    fn diff_from_journal_removal_keyset_roundtrip() {
        // 删除可追溯：before 有 x → diff 记 null；消费方按 null 删 x 后键集与 after 一致。
        let mut journal = HashMap::new();
        journal.insert("x".to_string(), Some(json!({"n": 1})));
        journal.insert("keep".to_string(), Some(json!(1)));
        let after = json!({"keep": 2});
        let diff = state_diff_from_journal(&journal, &after);
        assert_eq!(diff, json!({"x": null, "keep": 2}));

        // 净增净删不记：首触时 x 不存在（None），after 亦无 x → diff 不含 x。
        let mut journal = HashMap::new();
        journal.insert("x".to_string(), None);
        let diff = state_diff_from_journal(&journal, &json!({}));
        assert!(diff.as_object().is_some_and(|o| o.is_empty()));
    }
}

#[cfg(test)]
mod tests;
