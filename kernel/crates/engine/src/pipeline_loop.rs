// @feature: FP-0.2.〇 管道主循环(A1 粘滞改写/看门狗) | @ci: rust-test
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

use tracing::{debug, error, info, warn};

use agentos_hooks::{EventTarget, HookEventBus, LifecycleEvent};

use agentos_core::traits::{PluginInvoker, StorageBackend, STEP_CALL_TIMEOUT_CODE};
use agentos_core::types::{
    ContentLoader, EngineError, PluginContext, PluginError, PluginResult, RouteNext, TenantContext,
};

use crate::compiler::{
    CheckAction, CheckTrigger, CompiledBody, CompiledCheck, CompiledItem, CompiledPipeline,
    CompiledRoute, CompiledStep,
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

/// 单次插件调用超时兜底缺省（秒）：`invoke_pipeline_plugin` 单次 invoke 在
/// 配置/预算两档全部缺席时的唯一上界（冻结根因根修——缺省档恒在场，无界
/// 单次调用即根因本身）。取值依据：与 LLM 插件自身超时缺省对齐
/// （llm/_config_models.py：stream_idle_timeout 缺省 600s、非流式
/// call_timeout 缺省 300s）——健康调用（LLM 流式/工具/sidecar 冷启动）自有
/// 超时先于本值退路，本值只捕捉插件进程级挂死（自身超时机制失灵，装机
/// 2026-10-15 实证形态）。出口：state `plugin_call_timeout_seconds`。
/// run 墙钟预算内的 await 包装（超时看门狗执法点，见 [`PipelineExecutor::run_budget_remaining`]）：
/// 预算在场 → `timeout_seconds` 剩余量包界该 await，到点署名终态化并展开为
/// `None`（冻结的等待被放弃，run 按既有收尾路径折算 failed）；无预算 → 直通
/// （`Some(输出)`，行为与旧版一致）。调用方对 `None` 短路（break/return）。
/// 三段借用顺序化（预算读取共享借用 → future 可变借用 → 署名可变借用）故以
/// 宏文本展开参数化，不能写成函数（future 与 state 的借用无法同时作实参）。
macro_rules! budget_await {
    ($self:expr, $run_started:expr, $state:expr, $fut:expr) => {
        match $self.run_budget_remaining($run_started, $state) {
            Some(remaining) => match ::tokio::time::timeout(remaining, $fut).await {
                Ok(value) => Some(value),
                Err(_elapsed) => {
                    $self.force_timeout_signature($state);
                    None
                }
            },
            None => Some($fut.await),
        }
    };
}

/// 配置驱动的管道执行器。
///
/// 持有：
/// - `invoker`：调用原子插件（命中规则③）
/// - `project_root`：`{{path:...}}` 模板解析基准
/// - `default_tenant`：构造 `PluginContext` 时注入的租户上下文
/// - `plugin_ids`：已知插件 id 集合，`lookup_plugin` 在里面查（命中规则③判定）
/// - `store` / `run_id`：构造 `ContentLoader`（ADR ⑦）
pub struct PipelineExecutor {
    invoker: Arc<dyn PluginInvoker>,
    project_root: PathBuf,
    default_tenant: TenantContext,
    plugin_ids: HashSet<String>,
    store: Arc<dyn StorageBackend>,
    run_id: String,
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
    /// 并行 for-each 迭代局部的延迟 messages ops（内容移植自
    /// sync/open-main-20261008 c2db32c26）：迭代内不 apply——seq 分配/内存
    /// 数组/落库/实录/记账全由父循环按迭代序统一一次 apply。
    deferred_ops: parking_lot::Mutex<Vec<serde_json::Value>>,
    /// 并行迭代深度（0 = 顶层执行器；>0 = for-each 迭代执行器）：迭代内跳过
    /// step 轨迹/checkpoint/投影（父 step 收尾统一落，轨迹颗粒度=配置 step）。
    iteration_depth: u8,
    /// 监控 M2 分类声明（K2 声明化）：plugin_id → `capabilities.metrics_class`
    /// 声明解析结果。未注入/未声明 id 按 `Other` 计（编排类插件不计
    /// LLM/tool 指标）。
    metrics_classes: HashMap<String, PluginMetricsClass>,
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
    /// checks 触发纪元计数器（钩子收敛终局）：check 动作 `run:<步>` 每触发
    /// 一次 +1，触发步轨迹身份 = 模板步 id + 触发纪元（`"<步id>#<纪元>"`，
    /// 多次触发多条目不混叠，设计 §4.3 State 单一写面纪律①）。
    check_epoch: AtomicI64,
    /// state_delta 键级事件待消费队列（state 写路径发射，观察通知不携带
    /// 控制权）：journal_top_key 落点发射（窗口首触即记，含写前旧值），由
    /// 步边界 checks 分发消费——state 触发的 check 只消费命中键的事件
    /// （触发一次执行一次）。并行迭代执行器换新队列（迭代写面经父归并的
    /// journal 落点回到父队列，见 [`Self::merge_iteration`]）。
    pending_state_deltas: parking_lot::Mutex<Vec<StateDeltaEvent>>,
    /// 生命周期事件总线（观察路径）：run 开始/结束广播 OnPipelineStart/OnPipelineEnd
    /// 给审计/指标等订阅者。None = 零开销（未接线不发射）。
    hook_bus: Option<Arc<HookEventBus>>,
    /// 轮数硬上限（per-run 全部循环体的 while 轮累计）：while 条件恒真且终止
    /// 检查插件持续失败时的引擎层兜底，超限按 fail-closed 终止 run。0 = 非法
    /// 配置（run 入口报错拒绝）；缺省 [`DEFAULT_MAX_ROUNDS`]。
    max_rounds: usize,
    /// run 起始锚点（tokio 单调时钟，`run_compiled` 入口写入——与看门狗的
    /// `run_started` 同一 Instant 值，单一写入点无漂移）：单次调用超时的预算
    /// 档读这里（`invoke_plugin` 深在调用链下游，不持 `run_started` 参数）。
    /// None = 非 run 上下文（预算档缺席）；迭代执行器经 clone_for_iteration 继承。
    run_anchor: parking_lot::Mutex<Option<tokio::time::Instant>>,
}

/// state_delta 键级事件（state 写路径发射点 = journal_top_key 落点，设计
/// §4.3 引擎侧 P1 升级③）。仅通知不携带控制权：事件入待消费队列，由步边界
/// checks 分发消费（state 触发的 check 命中键即触发）；`new` 不在发射点取——
/// 窗口内多次写以消费时点的当前值为准（与轨迹 diff 的窗口语义一致）。
#[derive(Debug, Clone)]
struct StateDeltaEvent {
    /// 被写的顶层键。
    key: String,
    /// 写前旧值（窗口首触实录；None = 新增键）。
    old: Option<serde_json::Value>,
}

/// checks 分发边界（`execute_steps` 的步前/步后触发点）。
#[derive(Debug, Clone, Copy)]
enum CheckBoundary<'a> {
    /// 步执行前（`pre:<步 id>`）。
    Pre(&'a str),
    /// 步执行后（`post:<步 id>`）。
    Post(&'a str),
}

impl CheckBoundary<'_> {
    /// 是否匹配编译触达（pre/post 精确匹配步 id；state 触发不按边界匹配，
    /// 由 pending 事件命中判定）。
    fn matches(&self, trigger: &CheckTrigger) -> bool {
        match (self, trigger) {
            (CheckBoundary::Pre(step), CheckTrigger::Pre(id)) => step == id,
            (CheckBoundary::Post(step), CheckTrigger::Post(id)) => step == id,
            _ => false,
        }
    }
}

/// check 动作的流控后果（分发返回给 `execute_steps` 消费）。
enum CheckDivert {
    /// `route:<步>` 声明式转移：跳到本循环体指定步（跳数护栏调用方计）。
    Jump(String),
    /// `terminate`：已置 `ended=true`，当前循环体立即短路。
    Halted,
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
    /// * `run_id` - 当前运行实例 ID（关联标签，state.run_id 同源）
    pub fn new(
        invoker: Arc<dyn PluginInvoker>,
        project_root: PathBuf,
        default_tenant: TenantContext,
        plugin_ids: impl IntoIterator<Item = String>,
        store: Arc<dyn StorageBackend>,
        run_id: impl Into<String>,
    ) -> Self {
        Self {
            invoker,
            project_root,
            default_tenant,
            plugin_ids: plugin_ids.into_iter().collect(),
            store,
            run_id: run_id.into(),
            metrics: Arc::new(crate::metrics::EngineMetrics::new()),
            persistent_fields: HashSet::new(),
            metrics_classes: HashMap::new(),
            steps_since_checkpoint: AtomicI64::new(0),
            total_step_no: AtomicI64::new(0),
            ops_ledger: parking_lot::Mutex::new(Vec::new()),
            step_key_journal: parking_lot::Mutex::new(HashMap::new()),
            deferred_ops: parking_lot::Mutex::new(Vec::new()),
            iteration_depth: 0,
            pipeline_end_hooks: Vec::new(),
            round_events: None,
            round_counter: AtomicI64::new(0),
            check_epoch: AtomicI64::new(0),
            pending_state_deltas: parking_lot::Mutex::new(Vec::new()),
            hook_bus: None,
            max_rounds: DEFAULT_MAX_ROUNDS,
            run_anchor: parking_lot::Mutex::new(None),
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

    /// 监控 M2（K2 声明化）：注入插件 manifest `capabilities.metrics_class`
    /// 声明（`(plugin_id, 声明值)` 对）。生产侧从全部插件 manifest 收集；
    /// 不调用为空表（全部按 `Other` 计）。非法声明值 warn 后按未声明处理
    /// （`Other`）——指标少计是可观测债务，不构成行为故障。
    pub fn with_metrics_classes(
        mut self,
        declarations: impl IntoIterator<Item = (String, String)>,
    ) -> Self {
        for (plugin_id, declared) in declarations {
            match PluginMetricsClass::from_declared(&declared) {
                Some(class) => {
                    self.metrics_classes.insert(plugin_id, class);
                }
                None => warn!(
                    plugin = %plugin_id,
                    declared = %declared,
                    "capabilities.metrics_class 非法声明值，按 Other 计（合法值：llm/tool）"
                ),
            }
        }
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
        // 超时看门狗锚点（tokio 单调时钟；paused-clock 测试经 advance 注入可调
        // 延迟）：`timeout_seconds` 预算自 run 起始计（stop_check 同一契约：
        // 单次 run 执行 ≤ timeout_seconds），每执法点现读剩余量。
        let run_started = tokio::time::Instant::now();
        // 单次调用超时的预算档同锚（见 run_anchor 字段文档）：与看门狗同一
        // Instant 值复制，invoke_plugin 深层不持参数也能读剩余预算。
        *self.run_anchor.lock() = Some(run_started);
        // 轮数硬上限非法值检查（fail-closed）：0 = 无限轮，拒绝执行。
        if self.max_rounds == 0 {
            return Err(EngineError::Config {
                message: "max_rounds=0 非法（0 = 无限轮已被禁用；须为正整数）".to_string(),
            });
        }
        // 请求路径整组并行 respawn（Phase 3 #1）：run 到达即并行拉起管道链
        // 全部 sidecar 宿主并等就绪，后续步骤命中暖宿主——替代 idle GC 回收
        // 后逐槽串行懒 respawn 级联（首调税 19.6s/76% 主因，Phase0 基线台账
        // §八）。附加式 trait 方法默认 no-op；单宿主拉起失败只告警（步骤执行
        // 期懒 spawn 兜底，既有语义）；空引用集零开销。
        let referenced: Vec<String> = compiled.referenced_plugin_ids().into_iter().collect();
        if !referenced.is_empty() {
            self.invoker.ensure_hosts_ready(&referenced).await;
        }
        let mut state = initial_state;
        ensure_object(&mut state);
        // run 触发消息锚点（轮边界注入前捕获）：轮边界注入会把队列 user 消息
        // append 到触发消息之后，round_end 的 user 认领回传必须仍指向触发
        // 消息（前端乐观气泡按其 cmid 认领）——注入发生前在此定格。
        let triggering_user = last_role(&state, "user");
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

        // run 起手 sweep 前代流式占位行（ADR 2026-11-06-round-entry-slot-placeholder
        // 决策6）：上一进程/上一 run 死在出口之前的占位行（blob status=streaming、
        // run_id≠当前 run）在此收口——三空删行、有内容标 interrupted（内容保全，
        // 2026-11-04 消息出口权）。best-effort：失败 warn 不阻断 run，残留行由
        // 下一轮 run 起手重试收口。
        let sweep_pipeline = state
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        if !sweep_pipeline.is_empty() {
            match self
                .store
                .sweep_stale_streaming_slots(
                    &sweep_pipeline,
                    &self.default_tenant.tenant_id,
                    &self.run_id,
                )
                .await
            {
                Ok(summary) => {
                    if summary.deleted_empty + summary.marked_interrupted > 0 {
                        info!(
                            run_id = %self.run_id,
                            pipeline = %sweep_pipeline,
                            deleted_empty = summary.deleted_empty,
                            marked_interrupted = summary.marked_interrupted,
                            "run 起手 sweep 已收口前代流式占位行"
                        );
                    }
                }
                Err(e) => {
                    warn!(
                        run_id = %self.run_id,
                        pipeline = %sweep_pipeline,
                        error = %e,
                        "前代流式占位行 sweep 失败（残留行待下轮 run 收口，继续执行）"
                    );
                }
            }
        }

        // 首 token 前取消收口（缺陷A，台账 R1 ④immediate）：stop_generation 在
        // run 建行前到达时，dispatch_stop 落管道级 pending_cancel 标记——run
        // 创建起手在此消费：置 ended + user_requested 署名，循环体零执行直接
        // 收尾（终态映射 → Cancelled）。取消在任何时点都必须生效，禁静默吞。
        // 消费失败按未取消继续（best-effort，与 llm 起手检查同口径）。
        let pending_cancel_pipeline = state
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        if !pending_cancel_pipeline.is_empty() {
            match self
                .store
                .consume_pending_cancel(&pending_cancel_pipeline, &self.default_tenant.tenant_id)
                .await
            {
                Ok(true) => {
                    info!(
                        run_id = %self.run_id,
                        pipeline = %pending_cancel_pipeline,
                        "run 起手检出待取消标记（stop 先于 run 建行到达）：取消生效，零执行收尾"
                    );
                    self.journal_top_key(&state, "ended");
                    set_key(&mut state, "ended", serde_json::Value::Bool(true));
                    self.journal_top_key(&state, "router.stop_reason");
                    set_key(
                        &mut state,
                        "router.stop_reason",
                        serde_json::json!("user_requested"),
                    );
                }
                Ok(false) => {}
                Err(e) => warn!(
                    run_id = %self.run_id,
                    pipeline = %pending_cancel_pipeline,
                    error = %e,
                    "待取消标记消费失败（按未取消继续）"
                ),
            }
        }

        // 观察路径：run 开始广播 OnPipelineStart（审计/`lifecycle.pipeline_start_total`）。
        self.emit_pipeline_lifecycle(agentos_core::traits::LifecycleHook::OnPipelineStart, &state)
            .await;

        // ── 多循环体执行 ──
        // 轮数硬上限计数（per-run：全部循环体的 while 轮累计）。
        let mut rounds_used: usize = 0;
        // Err 终局同刀（孤儿工具卡合成）：循环体推进 Err（步骤护栏/路由死循环/
        // 轮数上限/轨迹落库耗尽）时 run 以 Err 上抛、api 层标 failed——上抛前
        // 先为在途 tool_calls 合成终态结果消息，error 终局与 interrupted 同刀
        // 恢复 tool_calls/tool 配对。
        if let Err(err) = self
            .advance_bodies(
                compiled,
                &mut state,
                &mut rounds_used,
                run_started,
                triggering_user,
            )
            .await
        {
            self.synthesize_orphan_tool_results(
                &mut state,
                &agentos_core::types::RunStatus::Failed,
            )
            .await;
            return Err(err);
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
        // 异常终局闸（凡异常终止必须显式报错）：无署名终局带异常特征时先
        // 署名失败词，再进终态映射——落库/轨迹/域事件三处同源折算 Failed。
        if let Some(reason) = abnormal_end_signature(&state) {
            error!(
                pipeline = %compiled.name,
                run_id = state.get("run_id").and_then(|v| v.as_str()).unwrap_or("?"),
                stop_reason = reason,
                "run 异常终局：无署名收束仍带未消费工具调用或未裁决空回复耗尽（多为 post 路由漏配），署名失败词折算 Failed",
            );
            self.journal_top_key(&state, "router.stop_reason");
            set_key(
                &mut state,
                "router.stop_reason",
                serde_json::Value::String(reason.to_string()),
            );
        }
        let terminal = agentos_core::types::RunStatus::from_control_state(&state);
        // 终局孤儿工具卡合成：interrupted/error 终局为在途 tool_calls 补终态
        // 结果消息（completed 不合成；suspended 等待恢复在途合法不合成）。
        // 置于 run_finalize 窗口前，合成 ops 随同窗轨迹落档（凡变更必可追溯）。
        self.synthesize_orphan_tool_results(&mut state, &terminal)
            .await;
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

    /// 多循环体推进主循环（自 run() 迁出的纯代码移动，行为零变化）：
    /// while 轮推进 + 循环体间转移决策。转移死循环防护：Phase 跳转/循环体数
    /// 上限的乘积保险。Err 上抛由调用方（run()）做 Err 终局合成后转发。
    async fn advance_bodies(
        &self,
        compiled: &CompiledPipeline,
        state: &mut serde_json::Value,
        rounds_used: &mut usize,
        run_started: tokio::time::Instant,
        triggering_user: Option<serde_json::Value>,
    ) -> Result<(), EngineError> {
        let max_guard = compiled.bodies.len().saturating_mul(4).max(16);
        let mut idx: usize = 0;
        let mut guard: usize = 0;
        while idx < compiled.bodies.len() {
            if guard > max_guard {
                return Err(EngineError::Other {
                    message: format!("循环体转移次数超限（{guard} 次，疑似 Phase 转移死循环）"),
                });
            }
            let body = &compiled.bodies[idx];
            // 挂起：整个管道等待恢复，不再推进任何循环体
            if truthy_flag(state, "suspended") {
                break;
            }
            // 插件可读 state["current_phase"] 按循环体分发（如 workspace_lifecycle）
            self.journal_top_key(state, "current_phase");
            set_key(state, "current_phase", serde_json::json!(body.id));
            // 体入口窗口：current_phase 切换进轨迹（凡 state 变更必可追溯）。
            self.append_window_trace("body_enter", state).await?;
            // 收尾语义：管道已 ended 时，run_on_error 循环体仍照常执行（忽略 ended）
            let ignore_ended = truthy_flag(state, "ended") && body.run_on_error;
            let iterations = self
                .execute_body(
                    body,
                    state,
                    compiled,
                    ignore_ended,
                    rounds_used,
                    run_started,
                    triggering_user.clone(),
                )
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
                self.journal_top_key(state, "next_phase");
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
            } else if !body.exit_routes.is_empty() && !truthy_flag(state, "suspended") {
                if let Some(matched) = self.apply_routes(&body.exit_routes, state) {
                    // apply_routes 的 Phase 分支会写 state.next_phase；此处已消费
                    // 返回值完成转移，立即移除，防止残留导致下一循环体结束时复跳。
                    // 消费删除同样 journal 定格（body_route 窗口记 null 标记）。
                    self.journal_top_key(state, "next_phase");
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
            self.append_window_trace("body_route", state).await?;
            if stop {
                break;
            }
            idx = jump.unwrap_or(idx + 1);
            guard += 1;
        }
        Ok(())
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
    ///
    /// `run_started`：超时看门狗锚点（run 起始单调时钟），经
    /// [`Self::run_budget_remaining`] 对轮次等待（轮次开/收事件、step 执行）
    /// 包界执法。
    #[allow(clippy::too_many_arguments)]
    async fn execute_body(
        &self,
        body: &CompiledBody,
        state: &mut serde_json::Value,
        compiled: &CompiledPipeline,
        ignore_ended: bool,
        rounds_used: &mut usize,
        run_started: tokio::time::Instant,
        triggering_user: Option<serde_json::Value>,
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
                        let Some(triple) =
                            budget_await!(self, run_started, state, self.round_start_event(state))
                        else {
                            break;
                        };
                        triple
                    }
                } else {
                    // 待输入队列轮边界注入（设计真值
                    // docs/working/待输入队列轮边界注入_方案_20261025.md）：
                    // LLM 调用迭代开轮前原子消费队列至空，队列消息以 user 消息
                    // 进入本轮请求。tool_execute 迭代不消费（该次不调大模型）。
                    let Some(()) =
                        budget_await!(self, run_started, state, self.inject_pending_inputs(state))
                    else {
                        break;
                    };
                    let Some(triple) =
                        budget_await!(self, run_started, state, self.round_start_event(state))
                    else {
                        break;
                    };
                    triple
                };
                self.execute_steps(
                    &body.steps,
                    &body.id,
                    state,
                    compiled,
                    ignore_ended,
                    run_started,
                )
                .await?;
                if count_role(state, "assistant") > assistant_before {
                    // 本轮（LLM 回合）产出 assistant → 发终态事件（new_message/stream_end）。
                    // 回合保持「打开」直到下一个 LLM 迭代（随后的工具迭代仍沿用本轮
                    // message_id——工具事件挂本轮消息，与 new_message.toolCalls 同卡位）。
                    let Some(()) = budget_await!(
                        self,
                        run_started,
                        state,
                        self.round_end_event(
                            state,
                            round_index,
                            round_id.clone(),
                            &round_start,
                            assistant_before,
                            triggering_user.clone(),
                        )
                    ) else {
                        break;
                    };
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
            let Some((round_index, round_id, round_start)) =
                budget_await!(self, run_started, state, self.round_start_event(state))
            else {
                return Ok(iteration);
            };
            self.execute_steps(
                &body.steps,
                &body.id,
                state,
                compiled,
                ignore_ended,
                run_started,
            )
            .await?;
            let Some(()) = budget_await!(
                self,
                run_started,
                state,
                self.round_end_event(
                    state,
                    round_index,
                    round_id,
                    &round_start,
                    assistant_before,
                    triggering_user,
                )
            ) else {
                return Ok(iteration);
            };
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
        // 入口槽位保留（ADR 2026-11-06-round-entry-slot-placeholder 决策1）：
        // 保留值 = 内存消息 max+1，与出口 append 的 resolve（apply_messages_op_update
        // 按 state 现有 max 分配 append seq）同基准——占位行只进 DB 不进内存数组，
        // 内存 max 不被它推进，出口投影 ON CONFLICT(tenant,pipeline,seq) 同槽
        // UPDATE 保入口 seq，绝不双行。seq 分配权单点（入口保留后投影不重排）。
        let reserved_seq = state
            .get("messages")
            .and_then(|v| v.as_array())
            .map(|a| {
                a.iter()
                    .filter_map(|m| m.get("seq").and_then(|s| s.as_i64()))
                    .max()
                    .unwrap_or(-1)
                    + 1
            })
            .unwrap_or(0);
        if let Some(obj) = state.as_object_mut() {
            obj.insert("message_id".into(), serde_json::json!(round_id));
            obj.insert("_assistant_id_assigned".into(), serde_json::json!(false));
            // 粘滞槽同步复位：上一轮认领槽位不得被本轮改写误粘滞（防跨轮 id 漂移）
            obj.insert("_assistant_id_slot_seq".into(), serde_json::Value::Null);
        }
        let start = crate::round_events::RoundStart {
            round_index,
            message_id: round_id.clone(),
            sequence: reserved_seq,
            pipeline_id: state_str(state, "pipeline_id").unwrap_or_default(),
            thread_id: state_str(state, "session_id")
                .or_else(|| state_str(state, "thread_id"))
                .unwrap_or_default(),
        };
        // 入口占位行落账（消息身份铸造点 = 槽位落账点）：流式进行中持久层
        // 从第一毫秒起有账——死在出口之前的尝试（Adapter/网关重试窗口、崩溃、
        // 强杀）留下带 seq 的可对账实体。观察点接线时才建（占位行服务于流式面，
        // 非流式路径零开销）。
        if self.round_events.is_some() {
            self.create_entry_placeholder(&start).await;
        }
        if let Some(ev) = &self.round_events {
            ev.on_round_start(start.clone()).await;
        }
        (round_index, round_id, start)
    }

    /// 入口占位行建行（ADR 2026-11-06 决策1）：message_slots 建占位行（blob
    /// `status=streaming`、空 content、record_id=本轮 message_id、run_id=当前
    /// run），经既有 op 通道 `apply_messages_ops_to_table` 单写入器落表——不走
    /// `apply_messages_op_update`（不进内存数组、不进实录/轨迹：占位行是 DB 侧
    /// 保留实体，轨迹回放重建不含它，崩溃残留由 run 起手 sweep 收口）。
    ///
    /// 失败不阻断轮次：error 留痕 + 计数后回落既有出口 INSERT 行为（降级 =
    /// 等价旧世界，非吞错；占位面故障不得放大为对话面故障）。
    async fn create_entry_placeholder(&self, start: &crate::round_events::RoundStart) {
        if start.pipeline_id.is_empty() {
            return;
        }
        let op = serde_json::json!({
            "op": "set",
            "seq": start.sequence,
            "msg": { "role": "assistant", "content": "", "status": "streaming" },
            "_message_id": start.message_id,
            "_run_id": self.run_id,
        });
        if let Err(e) = self
            .store
            .apply_messages_ops_to_table(
                &start.pipeline_id,
                &self.default_tenant.tenant_id,
                std::slice::from_ref(&op),
            )
            .await
        {
            error!(
                run_id = %self.run_id,
                pipeline = %start.pipeline_id,
                message_id = %start.message_id,
                seq = start.sequence,
                error = %e,
                "入口占位行建行失败（回落出口 INSERT 旧世界，占位面故障不放大为对话面故障）"
            );
            self.metrics.inc_persist_failure();
        }
    }

    /// 轮无 assistant 的占位行收口删行（ADR 2026-11-06 决策4/5）：真零产出
    /// （文本/思考/保留 tool_calls 三空，出口裁决在 llm_core，轮无 assistant 即
    /// 零产出）删行，占位行生命周期封闭于单轮、不跨轮复用。seq + record_id
    /// 双条件在 store 侧防误删（同槽被真实消息覆写时不命中）。失败 warn：残留
    /// 行由下轮 run 起手 sweep 收口。
    async fn delete_entry_placeholder(&self, start: &crate::round_events::RoundStart) {
        if start.pipeline_id.is_empty() {
            return;
        }
        match self
            .store
            .delete_streaming_placeholder(
                &start.pipeline_id,
                &self.default_tenant.tenant_id,
                start.sequence,
                &start.message_id,
            )
            .await
        {
            Ok(_) => {}
            Err(e) => {
                warn!(
                    run_id = %self.run_id,
                    pipeline = %start.pipeline_id,
                    message_id = %start.message_id,
                    seq = start.sequence,
                    error = %e,
                    "零产出轮占位行删除失败（残留行由 run 起手 sweep 收口）"
                );
            }
        }
    }

    /// 轮次结束边界：本轮新增了 assistant 才携带其完整持久形态（None = 本轮
    /// 无产出，消费方只发 stream_end 收尾，不发 new_message）。user 消息在
    /// 每轮都携带——消费方（api 桥接）只在首个有产出的轮次附认领回传。
    /// 发生过轮边界注入时携带 run 触发消息（注入会把队列消息排到触发消息
    /// 之后，last user 不再是触发消息；`_consumed_pending_cmids` 非空即本轮
    /// run 内发生过注入）；未注入 run 与既有行为逐字节一致。
    async fn round_end_event(
        &self,
        state: &serde_json::Value,
        round_index: i64,
        round_id: String,
        start: &crate::round_events::RoundStart,
        assistant_before: usize,
        triggering_user: Option<serde_json::Value>,
    ) {
        if let Some(ev) = &self.round_events {
            let assistant = if count_role(state, "assistant") > assistant_before {
                last_role(state, "assistant")
            } else {
                None
            };
            // 出口收口对称臂（决策4/5）：轮无 assistant = 真零产出 → 删除本轮
            // 占位行（生命周期必收口：fill 或 delete；不跨轮复用）。fill 臂由
            // 出口投影管辖（同槽 UPDATE）；删行是零产出专属收口。api 事件桥同
            // 判定点的删行为幂等双保险（行已删则桥侧不命中，no-op 不误删）。
            if assistant.is_none() {
                self.delete_entry_placeholder(start).await;
            }
            let user_message =
                if triggering_user.is_some() && key_present(state, "_consumed_pending_cmids") {
                    triggering_user
                } else {
                    last_role(state, "user")
                };
            ev.on_round_end(crate::round_events::RoundEnd {
                round_index,
                message_id: round_id,
                pipeline_id: start.pipeline_id.clone(),
                thread_id: start.thread_id.clone(),
                assistant,
                user_message,
            })
            .await;
        }
    }

    /// 待输入队列轮边界注入（设计真值
    /// docs/working/待输入队列轮边界注入_方案_20261025.md，用户裁定 2026-10-25）。
    ///
    /// LLM 调用迭代开轮前原子消费队列至空：每条记录以 user 消息 op append 进
    /// state.messages（[`apply_messages_op_update`] 内存+message_slots 同步落，
    /// 与 server.rs run 前 append op 同构），cmid 记入 run 内部键
    /// `_consumed_pending_cmids`（stage_finalize 提取进 EngineOutcome，api 层
    /// run 收尾据此批量通知 REST /chat 同步桥等待者），并即时回调
    /// `on_pending_inputs_consumed`（前端据此把队列条移入主消息流）。
    ///
    /// 参数面裁定 = 内容注入参数忽略：记录的 agent_id/thinking_strength/
    /// execution_context/state_overlay/pipeline_config_id 在注入场景不生效
    /// （在途 run 配置已定），仅在链级 drain 独立轮消费时生效。
    ///
    /// 落库失败不静默丢条目：apply 失败即原记录回队（created_at 原值保 FIFO
    /// 位）并停止本轮注入——下一轮边界自然重试；出队失败同理残留待下轮。
    async fn inject_pending_inputs(&self, state: &mut serde_json::Value) {
        let pipeline_id = state_str(state, "pipeline_id").unwrap_or_default();
        if pipeline_id.is_empty() {
            return;
        }
        let tenant_id = self.default_tenant.tenant_id.clone();
        let mut cmids: Vec<String> = Vec::new();
        let mut records: Vec<crate::round_events::ConsumedPendingRecord> = Vec::new();
        let mut ledgers: Vec<serde_json::Value> = Vec::new();
        loop {
            let rec = match self.store.pop_pending_input(&tenant_id, &pipeline_id).await {
                Ok(Some(rec)) => rec,
                Ok(None) => break,
                Err(e) => {
                    tracing::warn!(
                        pipeline = %pipeline_id,
                        error = %e,
                        "待输入队列轮边界出队失败（本轮停止注入，队列残留待下轮）"
                    );
                    break;
                }
            };
            let mut user_msg = serde_json::json!({ "role": "user", "content": rec.content });
            if !rec.client_message_id.is_empty() {
                user_msg["metadata"] =
                    serde_json::json!({ "client_message_id": rec.client_message_id });
            }
            let ops = [serde_json::json!({ "op": "set", "msg": user_msg, "_run_id": self.run_id })];
            match apply_messages_op_update(state, self.store.as_ref(), &tenant_id, &ops).await {
                Ok(ledger) => {
                    if !rec.client_message_id.is_empty() {
                        // 单 op 调用：首条 ledger 即本条 set 的权威坐标（set 条目
                        // 恒带 seq + message_id 内容指纹，op_ledger_entry）——认领
                        // 回传载荷，前端据此走 claim（终态必携排序键）。
                        let first = ledger.first();
                        records.push(crate::round_events::ConsumedPendingRecord {
                            client_message_id: rec.client_message_id.clone(),
                            message_id: first
                                .and_then(|e| e.get("message_id"))
                                .and_then(|v| v.as_str())
                                .unwrap_or_default()
                                .to_string(),
                            sequence: first
                                .and_then(|e| e.get("seq"))
                                .and_then(|v| v.as_i64())
                                .unwrap_or(0),
                            content: rec.content.clone(),
                        });
                        cmids.push(rec.client_message_id);
                    }
                    ledgers.extend(ledger);
                }
                Err(e) => {
                    tracing::error!(
                        pipeline = %pipeline_id,
                        input = %rec.id,
                        error = %e,
                        "注入消息落库失败，原记录回队（防静默丢失）"
                    );
                    if let Err(re) = self
                        .store
                        .enqueue_pending_input(&tenant_id, &pipeline_id, &rec)
                        .await
                    {
                        tracing::error!(
                            pipeline = %pipeline_id,
                            input = %rec.id,
                            error = %re,
                            "注入失败条目回队也失败，条目仅日志可追"
                        );
                    }
                    break;
                }
            }
        }
        if ledgers.is_empty() {
            return;
        }
        // 审计实录（对齐 persist_run_start 的 user_input 轨迹形态，ops 即轨迹）。
        use agentos_core::types::{PatchType, TraceEntry};
        let entry = TraceEntry {
            trace_id: format!("t_{}", uuid::Uuid::new_v4().simple()),
            pipeline_id: pipeline_id.clone(),
            seq: 0,
            plugin_id: "pending_input".to_string(),
            patch_type: PatchType::StateUpdate,
            patch_data: serde_json::json!({ "messages": { "_ops": ledgers } }),
            created_at: chrono::Utc::now().to_rfc3339(),
        };
        if let Err(e) = self.store.append_trace(entry).await {
            tracing::warn!(error = %e, "pending 注入实录落轨迹失败（消息本体已落库）");
        }
        if cmids.is_empty() {
            return;
        }
        if let Some(obj) = state.as_object_mut() {
            let arr = obj
                .entry("_consumed_pending_cmids".to_string())
                .or_insert_with(|| serde_json::Value::Array(vec![]));
            if let serde_json::Value::Array(a) = arr {
                a.extend(cmids.iter().map(|c| serde_json::json!(c)));
            }
        }
        if let Some(ev) = &self.round_events {
            ev.on_pending_inputs_consumed(crate::round_events::PendingConsumed {
                pipeline_id,
                client_message_ids: cmids,
                records,
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
    ///
    /// `run_started`：超时看门狗锚点——每个 step 执行（含其内部全部插件调用
    /// 与并行 for-each）被 `timeout_seconds` 剩余预算包界，到点署名短路。
    #[allow(clippy::too_many_arguments)]
    async fn execute_steps(
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
    fn execute_step<'a>(
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

    async fn execute_step_impl(
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
    fn clone_for_iteration(&self) -> Self {
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
            pipeline_end_hooks: self.pipeline_end_hooks.clone(),
            round_events: self.round_events.clone(),
            round_counter: AtomicI64::new(0),
            check_epoch: AtomicI64::new(self.check_epoch.load(Ordering::Relaxed)),
            pending_state_deltas: parking_lot::Mutex::new(Vec::new()),
            hook_bus: self.hook_bus.clone(),
            max_rounds: self.max_rounds,
            run_anchor: parking_lot::Mutex::new(*self.run_anchor.lock()),
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
    async fn merge_iteration(
        &self,
        state: &mut serde_json::Value,
        iter_exec: PipelineExecutor,
        iter_state: &serde_json::Value,
        loop_cfg: &agentos_core::types::LoopConfig,
        as_name: &str,
    ) {
        // ① messages ops
        let ops = std::mem::take(&mut *iter_exec.deferred_ops.lock());
        if !ops.is_empty() {
            let mut updates = HashMap::new();
            updates.insert("messages".to_string(), serde_json::json!({ "_ops": ops }));
            self.merge_and_project(state, &updates).await;
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
        // ④ _plugin_errors 拼接（引擎内部键，迭代错误逐迭代累积可见）
        if let Some(errs) = iter_state.get("_plugin_errors").and_then(|v| v.as_array()) {
            if !errs.is_empty() {
                let mut all = state
                    .get("_plugin_errors")
                    .and_then(|v| v.as_array())
                    .cloned()
                    .unwrap_or_default();
                all.extend(errs.iter().cloned());
                set_key(state, "_plugin_errors", serde_json::Value::Array(all));
            }
        }
        // ⑤ 终止/挂起标志：任一迭代置位即父置位
        for flag in ["ended", "suspended"] {
            if truthy_flag(iter_state, flag) && !truthy_flag(state, flag) {
                self.journal_top_key(state, flag);
                set_key(state, flag, serde_json::Value::Bool(true));
            }
        }
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
    async fn execute_parallel_for(
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
                        .await;
                }
            } else {
                // 有界并行快照：统一快照 + buffer_unordered，完成后按下标序归并。
                // 单迭代执行错误不炸整批：记为该迭代错误，归并照常，全部完成后上抛。
                let base_snapshot = state.clone();
                use futures::StreamExt;
                let mut results = futures::stream::iter(items.into_iter().enumerate())
                    .map(|(idx, item)| {
                        let iter_exec = self.clone_for_iteration();
                        let mut iter_state = base_snapshot.clone();
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
                        .await;
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
    async fn dispatch_boundary_checks(
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
    async fn run_triggered_step(
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
    fn journal_top_key(&self, state: &serde_json::Value, key: &str) {
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
    fn apply_state_key_update(
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
    /// `body_id` 透传给递归 composite（B 区 message 归属定位）。
    /// `check_trigger`：check 触发上下文（`_pipe_check` 约定字段，config 注入）——
    /// 递归全程透传（触发步的整棵执行子树都携带）；None = 顺序执行零改动。
    async fn execute_step_inner(
        &self,
        step: &CompiledStep,
        body_id: &str,
        state: &mut serde_json::Value,
        compiled: &CompiledPipeline,
        ignore_ended: bool,
        check_trigger: Option<&serde_json::Value>,
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
                    self.execute_step(
                        &target,
                        body_id,
                        state,
                        compiled,
                        ignore_ended,
                        check_trigger.cloned(),
                    )
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
                        .invoke_item_plugin(
                            plugin_id,
                            method.as_deref(),
                            inputs,
                            state,
                            check_trigger,
                        )
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
                        self.execute_step(
                            &target,
                            body_id,
                            state,
                            compiled,
                            ignore_ended,
                            check_trigger.cloned(),
                        )
                        .await?;
                    } else if self.lookup_plugin(&resolved) {
                        // 动态点无静态 inputs/method（模板运行时才定），传空
                        if self
                            .invoke_item_plugin(
                                &resolved,
                                None,
                                &HashMap::new(),
                                state,
                                check_trigger,
                            )
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
    /// `check_trigger`：check 触发上下文（Some 时 config 携带约定字段
    /// `_pipe_check`，触发步的插件调用可读 at/动作/state_delta 键级 payload）。
    ///
    /// 错误可见性（错误面单点）：插件失败——业务失败（result.error）与
    /// invoker 失败（Err，含 STEP_CALL_TIMEOUT）——统一收敛到
    /// [`Self::handle_plugin_failure`] 单点处理；`_plugin_errors` 记账与
    /// `plugin_step_failed` 域事件广播都从这一处出。
    /// 下划线前缀键不参与插件 state_updates 合并（merge 只写插件声明的键）。
    async fn invoke_item_plugin(
        &self,
        plugin_id: &str,
        method: Option<&str>,
        inputs: &HashMap<String, serde_json::Value>,
        state: &mut serde_json::Value,
        check_trigger: Option<&serde_json::Value>,
    ) -> bool {
        match self
            .invoke_plugin(plugin_id, method, inputs, &*state, check_trigger)
            .await
        {
            Ok(result) => {
                if let Some(err) = result.error.as_ref() {
                    self.handle_plugin_failure(state, plugin_id, err).await;
                    false
                } else {
                    // merge state_updates（轨迹在 step 级统一落，不钻插件）
                    // 分层持久化：merge 的同时投影到业务表（messages 增量、声明字段 upsert）
                    self.merge_and_project(state, &result.state_updates).await;
                    result.skip_remaining
                }
            }
            Err(e) => {
                self.handle_plugin_failure(state, plugin_id, &e).await;
                if e.code.as_deref() == Some(STEP_CALL_TIMEOUT_CODE) {
                    self.force_run_signature_if_budget_exhausted(state);
                }
                false
            }
        }
    }

    /// 插件失败单点处理（错误面单点收敛）：业务失败与 invoker 失败统一走此口
    /// ——warn + `_plugin_errors` 记账 + `plugin_step_failed` 域事件广播，三件
    /// 事只在这一处发生。步失败继续语义不变（ADR 2026-08-18：不按 error_policy
    /// 分发）。
    async fn handle_plugin_failure(
        &self,
        state: &mut serde_json::Value,
        plugin_id: &str,
        err: &PluginError,
    ) {
        warn!(
            plugin = %plugin_id,
            error = ?err,
            "plugin step failed, continuing (error_policy unified, ADR 2026-08-18)"
        );
        record_plugin_error(state, plugin_id, err);
        self.emit_plugin_failed_event(state, plugin_id, err);
    }

    /// 插件失败域事件广播（观察路径，best-effort、非阻塞）：`plugin_step_failed`
    /// 通用域事件（hooks::domain_event 单一载体，tags 带明细）——api 侧订阅翻译
    /// 为 WS `plugin_error` 实时面（此前该面只在 run 收尾结算，长跑 run 的步级
    /// 失败全程不可见，2026-10-27 装机压缩死循环实证）。总线未注入时零操作。
    fn emit_plugin_failed_event(
        &self,
        state: &serde_json::Value,
        plugin_id: &str,
        err: &PluginError,
    ) {
        let Some(bus) = self.hook_bus.as_ref() else {
            return;
        };
        let tags: Vec<(String, serde_json::Value)> = [
            ("pipeline_id", state_str(state, "pipeline_id")),
            ("thread_id", state_str(state, "session_id")),
            ("message_id", state_str(state, "message_id")),
            ("plugin_id", Some(plugin_id.to_string())),
            (
                "code",
                Some(
                    err.code
                        .clone()
                        .unwrap_or_else(|| "PLUGIN_EXEC_FAILED".into()),
                ),
            ),
            ("message", Some(err.message.clone())),
            ("run_id", Some(self.run_id.clone())),
        ]
        .into_iter()
        .map(|(k, v)| (k.to_string(), serde_json::json!(v)))
        .collect();
        bus.emit(agentos_hooks::domain_event("plugin_step_failed", tags));
    }

    /// 判定插件是否存在于已知插件集合（命中规则③）。
    fn lookup_plugin(&self, plugin_id: &str) -> bool {
        self.plugin_ids.contains(plugin_id)
    }

    /// run 墙钟预算剩余量（超时看门狗，`timeout_seconds` 语义：单次 run 执行
    /// ≤ 该秒数；agent_config_load 注入 / stop_check 插件同一配置源）。
    ///
    /// 键缺席、负值（stop_check 的 -1 无限制哨兵同款）或非有限数 = 不设防
    /// （`None`，行为与无看门狗完全一致）；0 = 立即到点。锚点 = run 起始
    /// （tokio 单调时钟，paused-clock 测试经 `advance` 注入可调延迟）；预算
    /// 每执法点现读——run 中途首次注入（首 prepare 的 agent_config_load）
    /// 即生效。剩余量饱和到 0：已过点返回 `Some(0)`，下一次包装立即到点。
    fn run_budget_remaining(
        &self,
        run_started: tokio::time::Instant,
        state: &serde_json::Value,
    ) -> Option<Duration> {
        let limit_secs = match state.get("timeout_seconds").and_then(|v| v.as_f64()) {
            Some(v) if v.is_finite() && v >= 0.0 => v,
            _ => return None,
        };
        let limit = Duration::from_secs_f64(limit_secs);
        Some(limit.saturating_sub(run_started.elapsed()))
    }

    /// 看门狗到点署名：写控制状态键（`ended` + `router.stop_reason="timeout"`
    /// ——词表内既有失败词，core `FAILED_STOP_REASONS` 冻结单点，引擎只查表
    /// 不加词），随 run 收尾既有路径（异常终局闸让位于已署名 →
    /// `from_control_state` → persist_run_end）折算 failed 落库。幂等：多处
    /// 到点重复调用重写同值。journal 进轨迹（凡 state 变更必可追溯）。
    fn force_timeout_signature(&self, state: &mut serde_json::Value) {
        error!(
            run_id = %self.run_id,
            "run 墙钟超时：timeout_seconds 预算耗尽，冻结的轮次等待被放弃，强制署名终态化（timeout）"
        );
        self.journal_top_key(state, "ended");
        set_key(state, "ended", serde_json::Value::Bool(true));
        self.journal_top_key(state, "router.stop_reason");
        set_key(state, "router.stop_reason", serde_json::json!("timeout"));
    }

    /// 单次插件调用上界的 **run 级封顶层**（原三分支的引擎侧剩余，见 ADR 草稿
    /// docs/working/2026-10-15-plugin-call-timeout-adr-draft.md）。基础策略档
    /// （manifest 自报 ∩ 缺省 600s，含 L1 让位——M3 单一解析点）在 invoker
    /// 单点（插件调用模块）——本层只管 run 级知识，二者嵌套合成、各管一事：
    ///
    /// - 配置档：state `plugin_call_timeout_seconds`（解析单源 =
    ///   [`agentos_invoker::invoker::state_plugin_call_timeout`]，M3 收敛：
    ///   state 键全仓只此一处解析，ADR 2026-11-05-timeout-taxonomy-and-merge；
    ///   有限数且 ≥0，负值/-1、非有限数按未配置——单次层不提供无限制出口，
    ///   无界单次调用即根因本身；载体与 `timeout_seconds` 同构：
    ///   agent_config_load 可注入、任意管道步 state_updates 可写）。
    /// - 预算档：run 剩余预算（`timeout_seconds` 看门狗同源同锚）。取 min
    ///   的不变量：**单次调用永不越过 run 剩余预算**（预算耗尽整 run 必死，
    ///   单次层先断只废一步）。
    ///
    /// 两档全缺席 → `None`（引擎不加壳，基础界由 invoker 恒在场兜底——
    /// 「所有路径恒有界」不变量由两层合成保证）。
    fn plugin_call_timeout(&self, state: &serde_json::Value) -> Option<Duration> {
        let configured = agentos_invoker::invoker::state_plugin_call_timeout(state);
        let budget = match *self.run_anchor.lock() {
            Some(anchor) => self.run_budget_remaining(anchor, state),
            None => None,
        };
        match (configured, budget) {
            (Some(c), Some(b)) => Some(c.min(b)),
            (Some(c), None) => Some(c),
            (None, Some(b)) => Some(b),
            (None, None) => None,
        }
    }

    /// 单次调用超时的错误信封（步失败路径入口）：`code` 取
    /// [`STEP_CALL_TIMEOUT_CODE`]，与 run 级 timeout 署名（`router.stop_reason`，
    /// FAILED_STOP_REASONS 词表）分面可辨——本信封进 `_plugin_errors` 后 run
    /// 继续推进（stop_check/熔断/轮上限接管），后者是 run 终态签名。
    fn step_call_timeout_error(plugin_id: &str, limit: Duration) -> PluginError {
        PluginError {
            message: format!(
                "单次插件调用超时（上界 {limit:?}）：invoke 等待被放弃（tokio drop 取消，\
                 sidecar 子进程交既有 idle-unload/respawn 兜底），步记失败、run 继续"
            ),
            code: Some(STEP_CALL_TIMEOUT_CODE.to_string()),
            source: Some(plugin_id.to_string()),
        }
    }

    /// 单次调用超时且预算档同步耗尽（剩余预算恰为 0）时代行 run 级署名：
    /// 复用看门狗 [`Self::force_timeout_signature`]（同签名同终态路径）。
    ///
    /// 为什么需要：单次包装是 step 级预算执法点的**内层** timeout，上界恰取
    /// 自剩余预算时两层同刻到点——tokio `Timeout::poll` 先查内层，内层先行
    /// 短路使外层看门狗执法点永远轮不到（其 inner 已就绪）。若无此代行，
    /// `timeout_seconds` 预算耗尽的 run 级 timeout 终态契约被单次层遮蔽
    /// （run 转由轮上限等兜底接管，署名失真）。配置/缺省档到点而预算未尽
    /// （剩余 > 0）或预算缺席时：不触发，步失败、run 继续（单次层本语义）。
    fn force_run_signature_if_budget_exhausted(&self, state: &mut serde_json::Value) {
        let exhausted = match *self.run_anchor.lock() {
            Some(anchor) => self.run_budget_remaining(anchor, state) == Some(Duration::ZERO),
            None => false,
        };
        if exhausted {
            self.force_timeout_signature(state);
        }
    }

    /// 按声明分类插件 id（K2 声明化：读 `capabilities.metrics_class`
    /// 装配的声明表；未声明 → `Other`，分类结果只进 metrics，不影响任何
    /// 执行路径）。
    fn plugin_metrics_class(&self, plugin_id: &str) -> PluginMetricsClass {
        self.metrics_classes
            .get(plugin_id)
            .copied()
            .unwrap_or(PluginMetricsClass::Other)
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
    /// `check_trigger`：checks 触发上下文（Some 时 config 注入约定字段
    /// `_pipe_check`——触发 at/动作/state_delta 键级 payload；None = 顺序执行
    /// 零改动）。
    ///
    /// `state` 为借用：invoke 期间引擎不写 state（结果在返回后 merge），
    /// 消除每步整包深拷。
    async fn invoke_plugin(
        &self,
        plugin_id: &str,
        method: Option<&str>,
        inputs: &HashMap<String, serde_json::Value>,
        state: &serde_json::Value,
        check_trigger: Option<&serde_json::Value>,
    ) -> Result<PluginResult, PluginError> {
        // 监控 M2：step 命中（每 invoke 一次 = 命中一个 step 的插件）
        self.metrics.inc_step_hit();
        let content_loader = ContentLoader::new(
            Arc::clone(&self.store),
            self.run_id.clone(),
            state_str(state, "pipeline_id").unwrap_or_default(),
        );
        let mut config = if inputs.is_empty() {
            serde_json::Value::Object(Default::default())
        } else {
            serde_json::json!({ "inputs": inputs })
        };
        if let Some(name) = method {
            config = agentos_invoker::shared::with_step_method(config, name);
        }
        if let Some(trigger) = check_trigger {
            if let Some(obj) = config.as_object_mut() {
                obj.insert("_pipe_check".to_string(), trigger.clone());
            }
        }
        let ctx = PluginContext::new(
            state,
            config,
            self.default_tenant.clone(),
            uuid::Uuid::nil(),
            content_loader,
        );
        // 监控 M2：LLM/工具调用次数 + 耗时（调度层视角 = invoke 前后差，
        // 监控设计 §补引擎调度层）。类别按 plugin.json
        // `capabilities.metrics_class` 声明（K2 声明化），未声明 = Other
        // 不计（编排类插件）。
        let class = self.plugin_metrics_class(plugin_id);
        let invoke_start = std::time::Instant::now();
        // 堆栈级诊断插桩：invoke await 即既有等待点（LLM 流式 / 工具阻塞——
        // human_interaction 在插件进程内等回复也显形于此），标签经任务本地
        // 槽 O(1) 更新（无槽上下文 no-op）。
        let pipeline_id = state_str(state, "pipeline_id").unwrap_or_default();
        agentos_core::task_activity::set_current_label(format!(
            "invoking plugin {plugin_id} (pipeline {pipeline_id})"
        ));
        // 冻结根因根修（两层合成、恒有界）：基础策略档（manifest 自报 ∩ 缺省
        // 600s）在 invoker 单点恒在场；本层只在 run 级配置/预算在场时加壳封顶
        // （[`Self::plugin_call_timeout`]）。到点放弃（tokio timeout drop
        // future = 最小取消语义），走步失败单点路径
        // （[`Self::handle_plugin_failure`]，code=STEP_CALL_TIMEOUT 与 run 级
        // timeout 署名可辨），run 不再被单次挂起冻结。
        let invoke_fut = self.invoker.invoke_pipeline_plugin(plugin_id, &ctx);
        let result = match self.plugin_call_timeout(state) {
            Some(limit) => match tokio::time::timeout(limit, invoke_fut).await {
                Ok(result) => result,
                Err(_elapsed) => {
                    warn!(
                        plugin = %plugin_id,
                        limit = ?limit,
                        "单次插件调用超预算：run 级封顶到点，步失败路径接管（STEP_CALL_TIMEOUT）"
                    );
                    Err(Self::step_call_timeout_error(plugin_id, limit))
                }
            },
            None => invoke_fut.await,
        };
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
    /// - 落库失败 warn + 计数软降级（对齐 merge_and_project 的 messages 写
    ///   失败语义）：终局点不因合成消息写失败掩盖原始终态。
    async fn synthesize_orphan_tool_results(
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
            self.apply_state_key_update(state, k, v.clone());
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

/// 合成 tool 结果消息的稳定机器标记（content 前缀）：interrupted/error 终局
/// 为孤儿 tool_calls 合成的结果消息一律以它开头——前端终态渲染与测试断言的
/// 共同锚点。措辞冻结：改动即破坏消费方契约。
pub const INTERRUPTED_TOOL_RESULT_MARKER: &str = "[interrupted]";

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
fn abnormal_end_signature(state: &serde_json::Value) -> Option<&'static str> {
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
/// 轨迹镜像定位符化的工具结果 staging 键（ADR 2026-09-29-trace-mirror-dedup）。
///
/// 这两键是 tool_core 每个工具调用 step 的 state_updates 全量重写体（数组整体
/// 换新），journal diff 按键忠实镜像即整份原文进 trace 补丁。全文权威副本：
/// `_full_tool_results` → tool_cache_writer 同 run 落工具缓存（缓存真值源）；
/// `tool_results` → 展示截断版随 messages 信封。轨迹只留摘要定位符。
/// DR 无损：回放消费链（merge_patch → merge_recovered_scalars）对这两键
/// （`_` 前缀键被跳过、展示版不参与恢复）零读取。
const TRACE_MIRROR_DIGEST_KEYS: &[&str] = &["_full_tool_results", "tool_results"];

/// 构造工具结果数组的轨迹摘要定位符：逐项留 tool_name + 序列化字节数，
/// 不留任何结果内容。数组输入 → `{"_trace_digest":true,count,items:[..]}`；
/// 非数组输入原样透传（防御：键值形状契约外的值不做有损变换）。
pub fn trace_mirror_digest(v: &serde_json::Value) -> serde_json::Value {
    let Some(items) = v.as_array() else {
        return v.clone();
    };
    let item_digests: Vec<serde_json::Value> = items
        .iter()
        .map(|it| {
            serde_json::json!({
                "tool_name": it.get("tool_name").and_then(|t| t.as_str()).unwrap_or(""),
                "chars": serde_json::to_string(it).map(|s| s.len()).unwrap_or(0),
            })
        })
        .collect();
    serde_json::json!({
        "_trace_digest": true,
        "count": items.len(),
        "items": item_digests,
    })
}

/// 管道不变上下文键清单：管道生命周期内内容不变的键。管道每轮 run 重建
/// state，初始化重写（agent_config_load/tool_schema/tool_core 等落键）对
/// step journal 恒为"新增键"→ 全值入 trace；实测 99.2% 实例为同管道字节级
/// 重复（装机库 1125 份 system_message 集中在 96 管道）。`context.` 前缀族
/// （context.agent_name 等）按前缀匹配，其余精确匹配。
const INVARIANT_SUPPRESS_KEYS: &[&str] = &[
    "system_message",
    "tool_output_contracts",
    "tool_schemas",
    "tool_ids",
];

/// 参与同值跳过的最小序列化字节数：小值照常更新（比对收益不值），基线表
/// 也不被海量小键污染。
const INVARIANT_SUPPRESS_MIN_BYTES: usize = 4096;

fn is_pipeline_invariant_key(key: &str) -> bool {
    INVARIANT_SUPPRESS_KEYS.contains(&key) || key.starts_with("context.")
}

/// 每管道不变键基线：`(pipeline_id, key) -> 内容哈希`。进程内全局——执行器
/// 每 run 重建，管道跨轮续用要求基线活过执行器实例；重启即清空自愈（重启后
/// 各活跃管道下一轮真写一次全值）。基线只服务于"同值跳过更新"的比对，state
/// 语义零影响；管道首条轨迹恒自含全值，本管道回放完整性不受影响（2026-08-30
/// 管道身份裁定零改动，恢复面无感知）。
fn global_invariant_baseline() -> &'static parking_lot::Mutex<HashMap<(String, String), u64>> {
    static BASELINE: std::sync::OnceLock<parking_lot::Mutex<HashMap<(String, String), u64>>> =
        std::sync::OnceLock::new();
    BASELINE.get_or_init(|| parking_lot::Mutex::new(HashMap::new()))
}

/// 同值不更新判定（更新层，2026-10-30）：不变清单键且序列化 ≥
/// [`INVARIANT_SUPPRESS_MIN_BYTES`] 且内容哈希与该管道基线一致 → true
/// （调用方**只跳 journal**——没有 journal 就没有 diff，也就没有轨迹，冗余
/// 在更新层消失，而非落库前剔除；set 照常落地，run state 每轮重建，跳写会让
/// 重建 state 永缺该键，见 [`Self::apply_state_key_update`]）。
/// 哈希不一致（真变更）/首写 → 登记基线并返回 false 照常
/// 更新。Null（删除标记）、小值、清单外键、空 pipeline_id（run 极早期，防
/// 跨管道误吞）恒 false。
fn invariant_update_suppressed(
    baseline: &mut HashMap<(String, String), u64>,
    pipeline_id: &str,
    key: &str,
    value: &serde_json::Value,
) -> bool {
    if pipeline_id.is_empty() || value.is_null() || !is_pipeline_invariant_key(key) {
        return false;
    }
    let serialized =
        serde_json::to_string(value).expect("serde_json Value serialization is infallible");
    if serialized.len() < INVARIANT_SUPPRESS_MIN_BYTES {
        return false;
    }
    use std::hash::{Hash, Hasher};
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    serialized.hash(&mut hasher);
    let content_hash = hasher.finish();
    let scope = (pipeline_id.to_string(), key.to_string());
    match baseline.get(&scope) {
        Some(&h) if h == content_hash => true,
        _ => {
            baseline.insert(scope, content_hash);
            false
        }
    }
}

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

/// A1：把内核 message_id 注入"本轮首个 assistant 追加 op"，并对该槽位本轮内的
/// 后续改写粘滞携带同一 id。
///
/// 注入以 `_message_id` 内部字段挂在 **op** 上而非消息体上：消息是不可变值
/// （blob 内容寻址、LLM 上下文、指纹实录都不应携带它），仅表侧 record_id 消费。
///
/// 认领命中条件（缺一不可，防止误注入历史消息）：
/// - `set` op 且 msg.role == "assistant" 且 msg 未自带 id；
/// - 新槽位追加（op.seq > resolve 前旧 max）——context_window_guard 等对旧槽位的
///   modify op 带显式旧 seq，天然排除；
/// - 每 run 仅一次：`state["_assistant_id_assigned"]` 置位后，多轮迭代的后续
///   assistant 追加（各自独立消息）不再注入，保证 id 与前端流式占位一一对应。
///
/// 粘滞改写：认领槽位 seq 记入 `state["_assistant_id_slot_seq"]`，本轮内对该槽
/// 的显式 seq 改写（post 步骤 [来源:] 标注、收尾改写等）重新注入同一 id——否则
/// 表侧 ON CONFLICT 改写把 record_id 回退成内容指纹，流式占位（a_）与 DB 重载
/// （mc_）键断裂，前端精确对账去重失败成双气泡（2026-10-19 偶发双气泡根因，
/// 装机库 pipeline 9b4da24d92e1 实证：最终回复 seq=11 record_id=mc_71eb12…）。
/// 粘滞槽随 round_start 每轮复位；未认领槽位（真历史/压缩改写）仍走内容派生。
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
    let assigned = state
        .get("_assistant_id_assigned")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let sticky_seq = state.get("_assistant_id_slot_seq").and_then(|v| v.as_i64());
    // 快路径：本轮 id 已认领且无粘滞槽 → 原样透传
    if assigned && sticky_seq.is_none() {
        return resolved.to_vec();
    }
    let mut out: Vec<serde_json::Value> = resolved.to_vec();
    let mut claimed_slot = sticky_seq;
    // 认领闸门：闩锁已置位或本批已认领一次即关闸（同批多轮 assistant 追加是
    // 各自独立消息，只有首个认领——原实现 break 语义的等价保持）
    let mut claim_open = !assigned;
    let mut newly_claimed = false;
    for op in out.iter_mut() {
        if op.get("op").and_then(|v| v.as_str()) != Some("set") {
            continue;
        }
        let op_seq = op.get("seq").and_then(|v| v.as_i64());
        let is_assistant_obj = op
            .get("msg")
            .map(|m| m.is_object() && m.get("role").and_then(|v| v.as_str()) == Some("assistant"))
            .unwrap_or(false);
        if claim_open {
            // 认领分支：本轮首个 assistant 新槽追加（条件缺一不可）
            let is_new_slot = op_seq.is_some_and(|s| s > entry_max_seq);
            let has_own_id = op
                .get("msg")
                .and_then(|m| m.get("id"))
                .and_then(|v| v.as_str())
                .is_some_and(|s| !s.is_empty());
            if is_new_slot && is_assistant_obj && !has_own_id {
                if let Some(obj) = op.as_object_mut() {
                    obj.insert("_message_id".to_string(), serde_json::json!(mid));
                }
                claimed_slot = op_seq;
                newly_claimed = true;
                claim_open = false;
                continue;
            }
        }
        // 粘滞分支：已认领槽位在本轮内的后续改写重新注入同一 id
        if claimed_slot.is_some() && op_seq == claimed_slot && is_assistant_obj {
            if let Some(obj) = op.as_object_mut() {
                obj.insert("_message_id".to_string(), serde_json::json!(mid));
            }
        }
    }
    if newly_claimed {
        if let Some(obj) = state.as_object_mut() {
            obj.insert(
                "_assistant_id_assigned".to_string(),
                serde_json::json!(true),
            );
            if let Some(ss) = claimed_slot {
                obj.insert("_assistant_id_slot_seq".to_string(), serde_json::json!(ss));
            }
        }
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

/// 监控 M2 插件指标类别（整改 K2 声明化：真值源 = plugin.json
/// `capabilities.metrics_class` 声明；内核内置 id→类别映射表已退役，
/// 内核零插件分类知识）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PluginMetricsClass {
    /// LLM 调用：计入 `llm_calls_total` / `llm_calls_micros`。
    Llm,
    /// 工具调用：计入 `tool_calls_total` / `tool_calls_micros`。
    Tool,
    /// 其他编排/服务插件：不计入 LLM/tool 指标。
    Other,
}

/// `capabilities.metrics_class` 合法声明值常量组（单一真值源，对齐 P-2
/// 鉴权常量组先例形态；`docs/schemas/plugin-manifest.schema.json` 的枚举
/// 与此同步）。
pub const METRICS_CLASS_LLM: &str = "llm";
pub const METRICS_CLASS_TOOL: &str = "tool";

impl PluginMetricsClass {
    /// 声明值 → 类别（精确匹配、大小写敏感）。`None` = 非法声明值
    /// （含显式 "other"——Other 是缺省语义，不是登记项）。
    pub fn from_declared(declared: &str) -> Option<Self> {
        match declared {
            METRICS_CLASS_LLM => Some(PluginMetricsClass::Llm),
            METRICS_CLASS_TOOL => Some(PluginMetricsClass::Tool),
            _ => None,
        }
    }
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
mod invariant_update_suppress_tests {
    use super::*;
    use serde_json::json;

    /// 构造 ≥ 阈值的可序列化值（文本体 8KB）。
    fn big_value(tag: &str) -> serde_json::Value {
        json!({"text": format!("{tag}-{}", "x".repeat(8192))})
    }

    /// 同管道同值：首判不抑制（登记基线、照常更新），再判抑制（同值不更新）。
    #[test]
    fn same_pipeline_same_value_suppressed_on_second_judgement() {
        let mut baseline = HashMap::new();
        assert!(
            !invariant_update_suppressed(&mut baseline, "p1", "system_message", &big_value("a")),
            "管道首写照常更新"
        );
        assert!(
            invariant_update_suppressed(&mut baseline, "p1", "system_message", &big_value("a")),
            "同管道同值重写 → 跳过更新（没有 update 就没有轨迹）"
        );
    }

    /// 值变更（哈希不一致）→ 不抑制且基线更新：同值跳过永不吞真变更。
    #[test]
    fn changed_value_not_suppressed_and_baseline_updates() {
        let mut baseline = HashMap::new();
        invariant_update_suppressed(&mut baseline, "p1", "system_message", &big_value("v1"));
        assert!(invariant_update_suppressed(
            &mut baseline,
            "p1",
            "system_message",
            &big_value("v1")
        ));
        assert!(
            !invariant_update_suppressed(&mut baseline, "p1", "system_message", &big_value("v2")),
            "变更照常更新"
        );
        // 基线已更新为 v2：回写 v1 也视为变更（基线只认最近一次应用值）
        assert!(!invariant_update_suppressed(
            &mut baseline,
            "p1",
            "system_message",
            &big_value("v1")
        ));
    }

    /// 裁定守护（2026-08-30 管道身份）：基线严格按 pipeline_id 隔离——不同
    /// 管道写同值互不影响，任何管道的首写恒更新，跨管道零耦合。
    #[test]
    fn different_pipeline_not_suppressed() {
        let mut baseline = HashMap::new();
        invariant_update_suppressed(&mut baseline, "p1", "system_message", &big_value("shared"));
        assert!(
            !invariant_update_suppressed(
                &mut baseline,
                "p2",
                "system_message",
                &big_value("shared")
            ),
            "他管道写过的同值不得抑制本管道更新"
        );
    }

    /// 边界：小值（<4KB）不参与；清单外键不受影响；context. 前缀族与精确键
    /// （tool_ids）命中；空 pipeline_id（run 极早期）恒不抑制（防跨管道误吞）。
    #[test]
    fn threshold_list_prefix_and_empty_pipeline_boundaries() {
        let mut baseline = HashMap::new();
        let small = json!({"text": "x".repeat(1024)});
        assert!(!invariant_update_suppressed(
            &mut baseline,
            "p1",
            "system_message",
            &small
        ));
        assert!(!invariant_update_suppressed(
            &mut baseline,
            "p1",
            "prompt",
            &big_value("u")
        ));
        assert!(!invariant_update_suppressed(
            &mut baseline,
            "",
            "system_message",
            &big_value("e")
        ));

        assert!(!invariant_update_suppressed(
            &mut baseline,
            "p1",
            "context.agent_name",
            &big_value("灵汐")
        ));
        assert!(invariant_update_suppressed(
            &mut baseline,
            "p1",
            "context.agent_name",
            &big_value("灵汐")
        ));
        assert!(!invariant_update_suppressed(
            &mut baseline,
            "p1",
            "tool_ids",
            &json!({"pad": "a".repeat(4096)})
        ));
        assert!(invariant_update_suppressed(
            &mut baseline,
            "p1",
            "tool_ids",
            &json!({"pad": "a".repeat(4096)})
        ));
    }

    /// Null（删除标记）恒不抑制：删除语义必须照常应用进 state 与轨迹。
    #[test]
    fn null_delete_marker_not_suppressed() {
        let mut baseline = HashMap::new();
        assert!(!invariant_update_suppressed(
            &mut baseline,
            "p1",
            "system_message",
            &serde_json::Value::Null
        ));
    }

    /// 全局基线接线：跨调用持久（同进程模拟跨轮），与纯函数语义一致。
    ///
    /// 不 clear 全局：并发测试互清共享基线致第二次调用被并发 clear 打回首写
    /// （llvm-cov 插桩窗口实测红，c4689c7b9 同族）。管道 scope ("pw", key)
    /// 仅本测试写入——唯一 scope 下无 residue 可防，clear 反成唯一互踩源。
    #[test]
    fn global_baseline_persists_across_calls() {
        assert!(!invariant_update_suppressed(
            &mut global_invariant_baseline().lock(),
            "pw",
            "tool_schemas",
            &big_value("w")
        ));
        assert!(invariant_update_suppressed(
            &mut global_invariant_baseline().lock(),
            "pw",
            "tool_schemas",
            &big_value("w")
        ));
    }
}

#[cfg(test)]
mod tests;
