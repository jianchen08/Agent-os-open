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
use std::sync::atomic::{AtomicI64, AtomicU32, Ordering};
use std::sync::Arc;
use std::time::{Duration, SystemTime};

use tracing::{debug, error, info, warn};

use agentos_hooks::{EventTarget, HookEventBus, LifecycleEvent};

use agentos_core::traits::{PluginInvoker, StorageBackend, STEP_CALL_TIMEOUT_CODE};
use agentos_core::types::{
    ContentLoader, EngineError, PluginContext, PluginError, PluginResult, RouteNext, TenantContext,
};

use self::finalize::abnormal_end_signature;
use self::finalize::count_role;
use self::finalize::ensure_object;
use self::finalize::key_present;
use self::finalize::last_role;
use self::finalize::set_key;
use self::finalize::state_str;
use self::finalize::truthy_flag;
use self::plugins::PluginMetricsClass;
use crate::compiler::{
    CheckAction, CheckTrigger, CompiledBody, CompiledCheck, CompiledItem, CompiledPipeline,
    CompiledRoute, CompiledStep,
};
use crate::condition::eval_expr;
use crate::template::{render_template, render_value};

/// 轮数硬上限缺省值（引擎层兜底，见 [`PipelineExecutor::with_max_rounds`]）。
const DEFAULT_MAX_ROUNDS: usize = 200;

/// LLM 连败熔断缺省阈值（引擎层兜底，见
/// [`PipelineExecutor::with_llm_fail_loop_max`]）：`metrics_class=llm` 插件步
/// 连续失败达该值即署名 `llm_fail_loop` 终态化。取值对齐 duplicate_check
/// 瞬态档（5，同工具熔断口径 2.5 倍裕度）——健康自愈实测 1-2 次内成功。
pub(crate) const DEFAULT_LLM_FAIL_LOOP_MAX: u32 = 5;

/// LLM 连败熔断阈值 env 覆盖键（常量默认 + env 覆盖惯例）：解析失败或 0 按
/// 缺省值处理（不提供禁用出口——连败熔断是僵死防线，装机 2026-11-18
/// 634adfc6a364 家族实证无熔断时全家僵尸轮询 80+ 分钟）。
pub const ENV_LLM_FAIL_LOOP_MAX: &str = "AGENTOS_LLM_FAIL_LOOP_MAX";

/// 插件级失败常驻镜像键（跨层契约：api 层 run 终态写入、引擎 run 起点清零
/// ——「当前 run 的错误面，run 起点即清零」，防上一 run 的失败残留在
/// 挂起/长跑 run 的面板详情卡上冒充当前错误；装机 2026-11-18
/// 634adfc6a364 实证：run1 PARSE_ERROR 残留被读面当 run2 现状）。
pub const RUN_PLUGIN_ERRORS_KEY: &str = "run.plugin_errors";

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
    /// 并行迭代共享基座：`messages`/管道不变量等只读大键只克隆一次，N 个
    /// 迭代共享（`Arc`）；迭代 state 只含写集克隆，插件投喂时按声明键回填。
    /// `None` = 无共享（串行/未启用/基座为空）。
    iteration_base: Option<Arc<serde_json::Map<String, serde_json::Value>>>,
    /// 监控 M2 分类声明（K2 声明化）：plugin_id → `capabilities.metrics_class`
    /// 声明解析结果。未注入/未声明 id 按 `Other` 计（编排类插件不计
    /// LLM/tool 指标）。
    metrics_classes: HashMap<String, PluginMetricsClass>,
    /// 声明了 `on_pipeline_end` 生命周期钩子的插件 id（manifest 收集）。
    ///
    /// run 结束时逐个 best-effort 分发 [`LifecycleHook::OnPipelineEnd`]（HookContext
    /// 携带 pipeline_id/run_id 标签）——spill_guard 的存档清理（native 经
    /// `hook_json` 约定字段收钩子删 `{base}/{pipeline_id}/`）依赖此通道。空集零开销。
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
    /// LLM 连败熔断计数器（`metrics_class=llm` 插件步连续失败次数，成功即
    /// 清零）：`Arc` 共享——并行 for-each 迭代执行器与顶层执行器同计一个
    /// run 的连败（LLM 面不可用是 run 级事实）。达 [`Self::llm_fail_loop_max`]
    /// 署名 `llm_fail_loop` 终态化（装机 2026-11-18 634adfc6a364 家族根修）。
    llm_fail_streak: Arc<AtomicU32>,
    /// LLM 连败熔断阈值（0/非法按缺省处理，见 [`DEFAULT_LLM_FAIL_LOOP_MAX`]）。
    llm_fail_loop_max: u32,
}

/// state_delta 键级事件（state 写路径发射点 = journal_top_key 落点，设计
/// §4.3 引擎侧 P1 升级③）。仅通知不携带控制权：事件入待消费队列，由步边界
/// checks 分发消费（state 触发的 check 命中键即触发）；`new` 不在发射点取——
/// 窗口内多次写以消费时点的当前值为准（与轨迹 diff 的窗口语义一致）。
#[derive(Debug, Clone)]
pub(crate) struct StateDeltaEvent {
    /// 被写的顶层键。
    key: String,
    /// 写前旧值（窗口首触实录；None = 新增键）。
    old: Option<serde_json::Value>,
}

/// checks 分发边界（`execute_steps` 的步前/步后触发点）。
#[derive(Debug, Clone, Copy)]
pub(crate) enum CheckBoundary<'a> {
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
pub(crate) enum CheckDivert {
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
            iteration_base: None,
            pipeline_end_hooks: Vec::new(),
            round_events: None,
            round_counter: AtomicI64::new(0),
            check_epoch: AtomicI64::new(0),
            pending_state_deltas: parking_lot::Mutex::new(Vec::new()),
            hook_bus: None,
            llm_fail_streak: Arc::new(AtomicU32::new(0)),
            llm_fail_loop_max: Self::resolve_llm_fail_loop_max(),
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

    /// 注入 LLM 连败熔断阈值（测试/定制注入；生产缺省走 env 覆盖 +
    /// [`DEFAULT_LLM_FAIL_LOOP_MAX`]，见 [`Self::resolve_llm_fail_loop_max`]）。
    /// `0` 非法按缺省处理——不提供禁用出口（无熔断 = 僵尸轮询防线缺口，
    /// 装机 2026-11-18 634adfc6a364 家族实证）。
    pub fn with_llm_fail_loop_max(mut self, max: u32) -> Self {
        self.llm_fail_loop_max = if max == 0 {
            DEFAULT_LLM_FAIL_LOOP_MAX
        } else {
            max
        };
        self
    }

    /// LLM 连败熔断阈值解析单点：env [`ENV_LLM_FAIL_LOOP_MAX`] 覆盖
    /// （解析失败/0 按缺省），缺省 [`DEFAULT_LLM_FAIL_LOOP_MAX`]。
    fn resolve_llm_fail_loop_max() -> u32 {
        match std::env::var(ENV_LLM_FAIL_LOOP_MAX) {
            Ok(raw) => match raw.trim().parse::<u32>() {
                Ok(v) if v > 0 => v,
                _ => {
                    warn!(
                        raw = %raw,
                        default = DEFAULT_LLM_FAIL_LOOP_MAX,
                        "LLM 连败熔断阈值 env 非法（非正整数），按缺省值处理"
                    );
                    DEFAULT_LLM_FAIL_LOOP_MAX
                }
            },
            Err(_) => DEFAULT_LLM_FAIL_LOOP_MAX,
        }
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
        // 清空上一轮残留的插件错误账本与物化结论（registry 热路径跨轮复用
        // state——不清理会让上一轮的失败被本轮 stage_finalize 重复提取）。
        // 运行期只维护按 (plugin_id, message) 聚合的有界账本
        // `_plugin_error_stats`；`_plugin_errors` 只在 run 收尾由
        // materialize_plugin_errors 物化一次。
        if key_present(&state, "_plugin_errors") {
            set_key(&mut state, "_plugin_errors", serde_json::json!([]));
        }
        if key_present(&state, finalize::PLUGIN_ERROR_STATS_KEY) {
            set_key(
                &mut state,
                finalize::PLUGIN_ERROR_STATS_KEY,
                serde_json::json!([]),
            );
        }
        // 插件级失败常驻镜像 run 起点清零（内存面，配合 persist_run_start 的
        // 常驻表清零）：冷恢复会把上一 run 的 run.plugin_errors 合并进 state，
        // 不清则随本轮 registry 快照回流读面（装机 2026-11-18 634adfc6a364
        // 实证：run1 PARSE_ERROR 残留伴 run2 挂起被详情卡冒充为当前错误）。
        if key_present(&state, RUN_PLUGIN_ERRORS_KEY) {
            self.journal_top_key(&state, RUN_PLUGIN_ERRORS_KEY);
            set_key(&mut state, RUN_PLUGIN_ERRORS_KEY, serde_json::json!([]));
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

        // 错误账本物化（用户裁定 2026-11-17）：运行期 state 只带按
        // (plugin_id, message) 聚合的有界账本；run 收尾一次性物化为
        // `_plugin_errors` 最终结论数组（≤ MAX_PLUGIN_ERROR_ENTRIES），供
        // api 层提取为 EngineOutcome.plugin_errors / run.plugin_errors 常驻镜像。
        finalize::materialize_plugin_errors(&mut state);

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
            // 本轮消息槽位（与 stream_start.sequence 同源）：tool_core 经 state
            // 读取并写进 tool_start/tool_result 事件载荷——工具卡宿主消息从第一
            // 刻起有权威排序坐标（工具事件先到/stream_start 丢失都不丢槽位，
            // 中断后不因缺 seq 垫底）。`_` 前缀=引擎私有键，不入 checkpoint
            // （store 侧 retain 过滤），随每轮开轮覆盖写。
            obj.insert("_message_seq".into(), serde_json::json!(reserved_seq));
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
            let mut user_msg =
                serde_json::json!({ "role": "user", "content": rec.request.content });
            if !rec.request.client_message_id.is_empty() {
                user_msg["metadata"] =
                    serde_json::json!({ "client_message_id": rec.request.client_message_id });
            }
            let ops = [serde_json::json!({ "op": "set", "msg": user_msg, "_run_id": self.run_id })];
            match apply_messages_op_update(state, self.store.as_ref(), &tenant_id, &ops).await {
                Ok(ledger) => {
                    if !rec.request.client_message_id.is_empty() {
                        // 单 op 调用：首条 ledger 即本条 set 的权威坐标（set 条目
                        // 恒带 seq + message_id 内容指纹，op_ledger_entry）——认领
                        // 回传载荷，前端据此走 claim（终态必携排序键）。
                        let first = ledger.first();
                        records.push(crate::round_events::ConsumedPendingRecord {
                            client_message_id: rec.request.client_message_id.clone(),
                            message_id: first
                                .and_then(|e| e.get("message_id"))
                                .and_then(|v| v.as_str())
                                .unwrap_or_default()
                                .to_string(),
                            sequence: first
                                .and_then(|e| e.get("seq"))
                                .and_then(|v| v.as_i64())
                                .unwrap_or(0),
                            content: rec.request.content.clone(),
                        });
                        cmids.push(rec.request.client_message_id);
                    }
                    ledgers.extend(ledger);
                }
                Err(e) => {
                    tracing::error!(
                        pipeline = %pipeline_id,
                        input = %rec.id,
                        error = %e,
                        "注入消息落库失败，原记录回队（防静默丢失；先落 DB 后动内存，\
                         内存零副作用——回队重注不产生重复消息）"
                    );
                    if let Err(re) = self.store.enqueue_pending_input(&tenant_id, &rec).await {
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
}

#[cfg(test)]
mod tests;

pub mod finalize;
pub use journal::{apply_messages_op_update, apply_slot_ops_to_array, trace_mirror_digest};
mod journal;
mod persist;
mod plugins;
mod state_io;
mod steps;
