//! 插件调用面（step_inner / invoke_plugin / 失败处理 / 预算与超时）。

use super::finalize::record_plugin_error;
use super::finalize::set_key;
use super::finalize::state_str;
use super::finalize::truthy_flag;
use super::*;

impl PipelineExecutor {
    /// 执行已编译列表项（G10：引用已在加载期解析为三类——插件 / composite / 动态模板）。
    ///
    /// 运行时只做：项级 when AST 求值（零解析）→ 按类别分派。composite 查统一步骤池
    /// 递归执行；动态模板项渲染后走同样的池/插件查找（显式保留的动态点）。
    /// `body_id` 透传给递归 composite（B 区 message 归属定位）。
    /// `check_trigger`：check 触发上下文（`_pipe_check` 约定字段，config 注入）——
    /// 递归全程透传（触发步的整棵执行子树都携带）；None = 顺序执行零改动。
    pub(crate) async fn execute_step_inner(
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
                        .await?
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
                            .await?
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
    pub(crate) async fn invoke_item_plugin(
        &self,
        plugin_id: &str,
        method: Option<&str>,
        inputs: &HashMap<String, serde_json::Value>,
        state: &mut serde_json::Value,
        check_trigger: Option<&serde_json::Value>,
    ) -> Result<bool, EngineError> {
        let is_llm_class = self.plugin_metrics_class(plugin_id) == PluginMetricsClass::Llm;
        match self
            .invoke_plugin(plugin_id, method, inputs, &*state, check_trigger)
            .await
        {
            Ok(result) => {
                if let Some(err) = result.error.as_ref() {
                    if is_llm_class {
                        self.note_llm_plugin_failure(state, plugin_id);
                    }
                    self.handle_plugin_failure(state, plugin_id, err).await;
                    Ok(false)
                } else {
                    if is_llm_class {
                        self.reset_llm_fail_streak();
                    }
                    // merge state_updates（轨迹在 step 级统一落，不钻插件）
                    // 分层持久化：merge 的同时投影到业务表（messages 增量、声明字段
                    // upsert）。messages 表写失败（先落 DB 后动内存，重试耗尽）=
                    // 持久化故障上抛终止 run——内存态与冷数据无分叉可降级，
                    // trace/投影写面同款硬语义。
                    self.merge_and_project(state, &result.state_updates).await?;
                    Ok(result.skip_remaining)
                }
            }
            Err(e) => {
                if is_llm_class {
                    self.note_llm_plugin_failure(state, plugin_id);
                }
                self.handle_plugin_failure(state, plugin_id, &e).await;
                if e.code.as_deref() == Some(STEP_CALL_TIMEOUT_CODE) {
                    self.force_run_signature_if_budget_exhausted(state);
                }
                Ok(false)
            }
        }
    }

    /// LLM 连败熔断计数 +1；达阈值即署名 `llm_fail_loop` 终态化（fail-closed，
    /// 与轮数硬上限同范式——写 `ended` + `router.stop_reason`，随 run 收尾既有
    /// 路径折算 failed）。
    ///
    /// 根因（装机 2026-11-18 634adfc6a364 家族，80+ 分钟僵尸轮询实证）：LLM
    /// 类插件步失败走 handle_plugin_failure 后 run 继续（ADR 2026-08-18），
    /// 插件层熔断器（duplicate_check LLM 连击）按错误文本指纹计数——gateway
    /// 错误文本逐调用易变（requestId/包装前缀），指纹恒异文，连击永不达阈；
    /// 失败者自身无 LLM 可决策停止，唯引擎能结构性终止。计数只看调用成败
    /// （不看文本），对任何错误形态稳健。成功即清零（见
    /// [`Self::reset_llm_fail_streak`]，自愈不误伤）。
    fn note_llm_plugin_failure(&self, state: &mut serde_json::Value, plugin_id: &str) {
        let streak = self.llm_fail_streak.fetch_add(1, Ordering::SeqCst) + 1;
        if streak < self.llm_fail_loop_max {
            return;
        }
        error!(
            run_id = %self.run_id,
            plugin = %plugin_id,
            streak = streak,
            max = self.llm_fail_loop_max,
            "LLM 连败熔断：LLM 类插件步连续失败达阈值，强制署名终态化（llm_fail_loop）"
        );
        self.journal_top_key(state, "ended");
        set_key(state, "ended", serde_json::Value::Bool(true));
        self.journal_top_key(state, "router.stop_reason");
        set_key(
            state,
            "router.stop_reason",
            serde_json::json!("llm_fail_loop"),
        );
    }

    /// LLM 连败计数清零（LLM 类插件调用成功即自愈，见
    /// [`Self::note_llm_plugin_failure`]）。
    fn reset_llm_fail_streak(&self) {
        self.llm_fail_streak.store(0, Ordering::SeqCst);
    }

    /// 插件失败单点处理（错误面单点收敛）：业务失败与 invoker 失败统一走此口
    /// ——warn + `_plugin_errors` 记账 + `plugin_step_failed` 域事件广播，三件
    /// 事只在这一处发生。步失败继续语义不变（ADR 2026-08-18：不按 error_policy
    /// 分发）。
    pub(crate) async fn handle_plugin_failure(
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
    pub(crate) fn emit_plugin_failed_event(
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
    pub(crate) fn lookup_plugin(&self, plugin_id: &str) -> bool {
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
    pub(crate) fn run_budget_remaining(
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
    pub(crate) fn force_timeout_signature(&self, state: &mut serde_json::Value) {
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
    pub(crate) fn plugin_call_timeout(&self, state: &serde_json::Value) -> Option<Duration> {
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
    pub(crate) fn step_call_timeout_error(plugin_id: &str, limit: Duration) -> PluginError {
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
    pub(crate) fn force_run_signature_if_budget_exhausted(&self, state: &mut serde_json::Value) {
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
    pub(super) fn plugin_metrics_class(&self, plugin_id: &str) -> PluginMetricsClass {
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
    pub(crate) async fn invoke_plugin(
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
        )
        // 并行迭代只读大键共享基座：迭代 state 缺失的声明键由 invoker 从
        // 基座回填（插件视角与全量 state 一致，内存成本与 state 总量解耦）。
        .with_base_state(self.iteration_base.as_deref());
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
}

/// 监控 M2 插件指标类别（整改 K2 声明化：真值源 = plugin.json
/// `capabilities.metrics_class` 声明；内核内置 id→类别映射表已退役，
/// 内核零插件分类知识）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum PluginMetricsClass {
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
pub(super) const METRICS_CLASS_LLM: &str = "llm";
pub(super) const METRICS_CLASS_TOOL: &str = "tool";

impl PluginMetricsClass {
    /// 声明值 → 类别（精确匹配、大小写敏感）。`None` = 非法声明值
    /// （含显式 "other"——Other 是缺省语义，不是登记项）。
    pub(super) fn from_declared(declared: &str) -> Option<Self> {
        match declared {
            METRICS_CLASS_LLM => Some(PluginMetricsClass::Llm),
            METRICS_CLASS_TOOL => Some(PluginMetricsClass::Tool),
            _ => None,
        }
    }
}
