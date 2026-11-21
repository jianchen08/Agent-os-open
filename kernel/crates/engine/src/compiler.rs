//! # 管道加载期编译器（G10）
//!
//! 把 [`PipelineConfig`]（含公共 step 库）在**加载期一次性编译**成
//! [`CompiledPipeline`] 执行计划，运行时零解析、零三级命中重算：
//!
//! 1. **when 表达式预编译**：所有门/路由条件在编译期 tokenize + parse 成
//!    [`condition::Expr`] AST（`eval_condition` 每次调用都重 parse 的税被消灭）；
//!    语法错误在加载期报错（带 step/body 定位），不再"静默 false 死路由"。
//! 2. **四级命中静态解析**：step 列表项的引用在编译期判定为
//!    [`CompiledItem::Plugin`]（③ 已注册步骤服务 / ④ 插件名直引）/
//!    [`CompiledItem::Composite`]（管道/库 step，运行时查统一步骤池递归）/
//!    [`CompiledItem::Dynamic`]（`{{state.xxx}}` 模板名，运行时渲染后查池/插件——
//!    显式保留的动态点）；未知引用在加载期报错。传入 [`StepServiceIndex`] 时
//!    （[`compile_pipeline_with_hooks`] / [`compile_pipeline_with_checks`] 的
//!    `index` 参数），③ 级命中携带解析结果 `method`（SDK 侧按 `_step_method`
//!    约定字段分发，提案 §3.4）；复合体隐式诉求（插件 id 存在但索引未注册）→
//!    编译期报错（fail-closed 强制自白，提案 §3.3）；未传索引（legacy 入口
//!    [`compile_pipeline`]）→ 现状插件名直引（method None = 默认 execute 入口）。
//! 3. **步骤服务索引**：聚合全部启用插件 manifests 的 `capabilities.steps`（显式）
//!    与单入口插件隐式默认注册（服务化提案 §3.3），名字全局查重；复合体
//!    （tools/services/steps）隐式关闭——管道按插件 id 直引 → 编译错误（强制自白）。
//! 4. **checks 统一检测声明**（钩子收敛终局，设计真值
//!    `docs/working/DSH生态嫁接通用机制设计_20261104.md` §4.3）：统一模型 =
//!    管道 = 流（顺序 steps + then 跳转）+ checks（触发时机 × when × 动作）。
//!    声明经 [`compile_pipeline_with_checks`] 编译为 [`CompiledCheck`] 分发表
//!    （触发时机 `pre/post/state` × when AST × 动作 `route/terminate/run`），
//!    route/run 目标编译期存在性校验，安全关键步骤护栏（不可被 checks 触发/
//!    跳过）在此执法；空集零开销。hooks 机制退役：非空 hooks 声明编译报错
//!    （[`HookFile`] 类型仅为 api 加载面契约保留）。
//! 5. **引用环检测**：composite 递归引用图 DFS，环 → 编译错误（运行时递归不死循环）。
//! 6. **转移目标校验**：`RouteNext::Phase/Step` 目标存在性在此复核（api 层
//!    `validate_no_name_conflicts` 已查 Phase，此处双保险 + Step 目标）。
//!
//! 编译是纯函数（`compile_pipeline`），无 IO 无时序，可同步单测。
//! 生产路径：启动期 / 热重载时编译一次 → `Arc<CompiledPipeline>` 原子换入，
//! 在途 run 持旧计划跑完（快照语义），新 run 取新计划。

use std::collections::{HashMap, HashSet};

use agentos_core::traits::PluginManifest;
use agentos_core::types::{
    CheckpointConfig, LoopBody, LoopConfig, PipelineConfig, PipelineStep, Route, RouteNext,
    StepCheck, StepLibrary,
};
use sha2::{Digest, Sha256};

use crate::condition::{parse_condition, Expr};

/// 编译期错误（带定位上下文，加载期暴露，不静默）。
#[derive(Debug, Clone)]
pub struct CompileError {
    /// 定位串（如 `"循环体 'main' / step 'post'"`）。
    pub location: String,
    /// 错误描述。
    pub message: String,
}

impl std::fmt::Display for CompileError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.location, self.message)
    }
}

/// 编译后的管道（运行时零解析）。
///
/// - `bodies`：各循环体的编译形态（steps 已解析、when 已 AST 化、转移已解析）。
/// - `steps` / `step_index`：统一步骤池——管道 step + 库 step（管道优先），
///   [`CompiledItem::Composite`] 与动态项运行时查池。
#[derive(Debug, Clone)]
pub struct CompiledPipeline {
    pub name: String,
    pub bodies: Vec<CompiledBody>,
    pub checkpoint: CheckpointConfig,
    /// 源 [`PipelineConfig`] 的确定性指纹（[`pipeline_config_hash`]，SHA-256 前 16 hex）。
    /// 编译期一次算好随计划走，run 落 runs.config_hash 用。
    pub config_hash: String,
    /// checks 分发表（钩子收敛终局，设计 §4.3）：checks 统一检测声明的编译
    /// 产物，运行时按（循环体, 触发时机）查表消费。空 = 未声明任何 check
    /// （边界分发点零开销短路）。
    pub checks: Vec<CompiledCheck>,
    /// 管道配置声明的初始 state 缺省（P1-1 声明驱动缺省）：执行首轮前种入的
    /// 标量键值（api 层消费；键已存在时不覆盖——本轮值优先）。
    pub initial_state: Vec<(String, serde_json::Value)>,
    /// 主轮循环轮数硬上限（管道 YAML `max_rounds:` 声明，ADR
    /// 2026-09-11-engine-loop-cap）：`None` = 未声明，api 层不注入，executor
    /// 用缺省 200；`Some(0)` 透传给 executor 由 run 入口报错拒绝。
    pub max_rounds: Option<usize>,
    steps: Vec<CompiledStep>,
    step_index: HashMap<String, usize>,
}

impl CompiledPipeline {
    /// 按 id 查统一步骤池（动态项运行时解析用）。
    pub fn find_step(&self, id: &str) -> Option<&CompiledStep> {
        self.step_index.get(id).map(|&i| &self.steps[i])
    }

    /// 按 id 定位循环体下标（转移跳转用）。
    pub fn body_index(&self, id: &str) -> Option<usize> {
        self.bodies.iter().position(|b| b.id == id)
    }

    /// 全部循环体 id（供外部诊断）。
    pub fn body_ids(&self) -> Vec<&str> {
        self.bodies.iter().map(|b| b.id.as_str()).collect()
    }

    /// 查 checks 分发表：`(循环体, 触发时机)` → 命中条目（引用，声明序）。
    ///
    /// 空表（未声明任何 check）提前返回空 Vec——分发点先查表再决定是否进入
    /// 分发分支，无 checks 配置的主干零开销（空集短路）。
    pub fn checks_for_trigger(&self, body_id: &str, trigger: &CheckTrigger) -> Vec<&CompiledCheck> {
        if self.checks.is_empty() {
            return Vec::new();
        }
        self.checks
            .iter()
            .filter(|c| c.body_id == body_id && &c.trigger == trigger)
            .collect()
    }

    /// 编译产物引用的全部插件 id——boot sidecar 预热集的单一来源。
    ///
    /// 覆盖：各循环体步骤项（`Plugin` 项；`Composite` 项查统一步骤池递归展开，
    /// 步骤 id 级 visited 防御池内环）。checks 动作目标是循环体 step，其引用
    /// 已经由步骤项覆盖。`Dynamic` 项运行时才解析出插件名，不在此列（懒 spawn 兜底）。
    pub fn referenced_plugin_ids(&self) -> HashSet<String> {
        fn visit(
            step: &CompiledStep,
            pipe: &CompiledPipeline,
            ids: &mut HashSet<String>,
            seen: &mut HashSet<String>,
        ) {
            if !seen.insert(step.id.clone()) {
                return;
            }
            for item in &step.items {
                match item {
                    CompiledItem::Plugin { plugin_id, .. } => {
                        ids.insert(plugin_id.clone());
                    }
                    CompiledItem::Composite { step_id, .. } => {
                        if let Some(sub) = pipe.find_step(step_id) {
                            visit(sub, pipe, ids, seen);
                        }
                    }
                    CompiledItem::Dynamic { .. } => {}
                }
            }
        }
        let mut ids = HashSet::new();
        for body in &self.bodies {
            for step in &body.steps {
                let mut seen = HashSet::new();
                visit(step, self, &mut ids, &mut seen);
            }
        }
        // checks 动作目标 = 本循环体 step（route/run 目标编译期已校验存在），
        // 其插件引用已经由循环体步骤项收集，不在此重复展开。
        ids
    }
}

/// 步骤服务注册表（服务化提案 §3.2 第③级命中的数据层）。
///
/// 编译期聚合全部启用插件 manifests 的 `capabilities.steps`（显式条目）与
/// 单入口插件的隐式默认注册（§3.3），名字全局查重；运行时按 step 名查
/// `{plugin_id, method}` 静态绑定，零反射。
#[derive(Debug, Clone, Default)]
pub struct StepServiceIndex {
    entries: HashMap<String, ResolvedStep>,
}

/// 已解析的步骤服务：step 名 → 实现定位。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedStep {
    /// 提供该步骤服务的插件 id。
    pub plugin_id: String,
    /// 目标方法标识：`Some(步骤 name)` = 显式注册（SDK 侧按 name 分发，
    /// sidecar 经 ctx 约定字段 `_step_method` 透传）；`None` = 默认 execute
    /// 入口（隐式默认注册，§3.3）。
    pub method: Option<String>,
}

impl StepServiceIndex {
    /// 按 step 名查已注册步骤服务（命中规则③）。
    pub fn resolve(&self, step_name: &str) -> Option<&ResolvedStep> {
        self.entries.get(step_name)
    }

    /// 全部已注册 step 名（诊断/测试用）。
    pub fn names(&self) -> Vec<&str> {
        let mut names: Vec<&str> = self.entries.keys().map(|s| s.as_str()).collect();
        names.sort();
        names
    }
}

/// 聚合全部启用插件 manifests 构建步骤服务索引（服务化提案 §3.2/§3.3）。
///
/// 生产接线（ADR 2026-10-08-step-service-level3-wiring）：api 层
/// `load_and_compile_by_name` 恒构索引传入 with_hooks 装配——③级命中生产
/// 可达，触发条件（步骤服务跨插件编排需求立项）已现，死层①裁定
/// （ADR 2026-09-09-kernel-dead-layer-adjudication）的接线分支就此执行；
/// hooks 沿传空（无声明需求），hooks 链仍按原裁定保留。
///
/// 规则：
/// - **显式条目**：`capabilities.steps[].name` → `{plugin_id, method}`——
///   异名步骤 `method: Some(name)`（SDK 侧按 name 分发，不发明 type 字段）；
///   **name == 插件 id 的条目映射 `method: None`**（默认 execute 语义）——
///   现网 manifests 通用声明形态即 `[{name: "pipeline_<id>"}]`，该映射使
///   ③级命中与④插件名直引完全等价，存量声明零破坏；
/// - **隐式默认**：未声明 steps 且无 tools/services 的插件自动生成
///   `{name: <插件id>, plugin_id: <插件id>, method: None}`（None = 默认 execute
///   入口）；声明了 tools/services 的复合体隐式关闭——管道按插件 id 直引 →
///   编译错误（fail-closed，强制自白条款）；
/// - **名字全局查重**：显式之间、显式与隐式之间冲突 → 编译错误（含双方来源 id）。
///
/// 纯函数（无 IO 无时序），可同步单测。
pub fn build_step_service_index(
    manifests: &[PluginManifest],
) -> Result<StepServiceIndex, CompileError> {
    let mut entries: HashMap<String, ResolvedStep> = HashMap::new();
    for manifest in manifests {
        let caps = &manifest.capabilities;
        let explicit = &caps.steps;
        let composite = !caps.tools.is_empty() || !caps.services.is_empty();
        if explicit.is_empty() && !composite {
            // 隐式默认注册：单入口纯步骤插件（无 tools/services/steps），
            // name = 插件 id，method = None（默认 execute 入口）。
            let entry = ResolvedStep {
                plugin_id: manifest.id.clone(),
                method: None,
            };
            if let Some(prev) = entries.insert(manifest.id.clone(), entry) {
                return Err(CompileError {
                    location: "步骤服务索引".to_string(),
                    message: format!(
                        "步骤服务名 '{}' 冲突：插件 '{}' 的隐式默认注册与插件 '{}' 的注册重复",
                        manifest.id, manifest.id, prev.plugin_id
                    ),
                });
            }
            continue;
        }
        for step in explicit {
            // name == 插件 id = 该插件默认入口的自白式声明（现网通用形态），
            // 映射 method None 保持与④直引等价；异名步骤才走具名分发。
            let method = if step.name == manifest.id {
                None
            } else {
                Some(step.name.clone())
            };
            let entry = ResolvedStep {
                plugin_id: manifest.id.clone(),
                method,
            };
            if let Some(prev) = entries.insert(step.name.clone(), entry) {
                return Err(CompileError {
                    location: "步骤服务索引".to_string(),
                    message: format!(
                        "步骤服务名 '{}' 冲突：插件 '{}' 与插件 '{}' 重复声明",
                        step.name, manifest.id, prev.plugin_id
                    ),
                });
            }
        }
    }
    Ok(StepServiceIndex { entries })
}

/// 编译后的 check 触发时机。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CheckTrigger {
    /// 步执行前触发（`pre:<step id>`）。
    Pre(String),
    /// 步执行后触发（`post:<step id>`；步产出路由优先，check route 在步已
    /// 产出路由时抑制）。
    Post(String),
    /// state 顶层键写事件触发（`state:<键>`）：键被写（[`crate::pipeline_loop`]
    /// 写路径 journal 落点发射 state_delta 事件）后由首个步边界消费——
    /// 触发一次执行一次。when 可表达 equals 值条件（求值时读当前 state）。
    State(String),
}

/// 编译后的 check 动作。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CheckAction {
    /// 声明式转移（`route:<step id>`，目标 = 同循环体 step；执行时与 step 级
    /// Step 跳转同语义，共享跳数护栏 steps×4）。
    Route(String),
    /// 终止当前循环体（`terminate`；原钩子 `decision:terminate` 语义平移——
    /// 置 `ended=true`，循环体立即终止，后续循环体/收尾体照常推进）。
    Terminate,
    /// 一次性触发步（`run:<step id>`，目标 = 同循环体 step）：触发一次执行
    /// 一次，与顺序步完全同语义（经同一 invoke/轨迹/checkpoint 面）；轨迹
    /// 身份 = 模板步 id + 触发纪元（多次触发多条目不混叠）。
    Run(String),
}

/// 编译后的 check 条目（[`compile_pipeline_with_checks`] 产物，分发表行）。
#[derive(Debug, Clone)]
pub struct CompiledCheck {
    /// 宿主循环体 id（触发与动作目标都在该循环体内）。
    pub body_id: String,
    pub trigger: CheckTrigger,
    /// 触发谓词 AST（None = 恒真；假则本 check 不触发）。
    pub when: Option<Expr>,
    pub action: CheckAction,
}

impl CompiledCheck {
    /// 触发时机人读标签（`pre:s1` / `post:s1` / `state:x`）——诊断与轨迹
    /// 归属来源标注（`check@<at>` 窗口 id）共用。
    pub fn at_label(&self) -> String {
        match &self.trigger {
            CheckTrigger::Pre(id) => format!("pre:{id}"),
            CheckTrigger::Post(id) => format!("post:{id}"),
            CheckTrigger::State(key) => format!("state:{key}"),
        }
    }
}

/// 管道 YAML 的 hooks 声明形态（`{on: <事件>, run: <插件id>[.<method>]}`）。
///
/// **已退役**（钩子收敛终局，设计 §4.3：hook 不再独立存在，统一模型 =
/// checks 分发 + run 步）。本类型仅为 api 加载面契约保留（api `pipeline_loader`
/// 解析 YAML `hooks:` 字段的载体）；声明进入编译即报错（见
/// [`compile_pipeline_with_hooks`]），迁移映射：step 级
/// `{on: step_end, run}` → `{at: "post:<step>", then: "run:<步骤id>"}`。
#[derive(Debug, Clone, serde::Deserialize)]
pub struct HookFile {
    pub on: String,
    pub run: String,
}

/// 编译管道 + checks 分发表 + 步骤服务接线（钩子收敛终局的统一编译入口）。
///
/// 在 `compile_pipeline_with_index` 基础上编译 checks 声明：
///
/// - `body_checks`：各循环体已反序列化的 checks 声明（调用方归一传入，键为
///   循环体 id）；未声明传空切片，产物 `checks` 为空 Vec（分发点零开销短路）；
/// - `safety_locked_steps`：安全关键步骤 id 集（审批/评估等——不可被 checks
///   触发或跳过的护栏标记集；装载面后续接线，引擎先执法）。
///
/// 校验（全部编译期 fail-closed）：
/// - `at` 词表：`pre:<步>` / `post:<步>` / `state:<键>`，触发步须在本循环体；
/// - `then` 词表：`route:<步>` / `terminate` / `run:<步>`，目标步须在本循环体；
/// - when 语法错误即报错（AST 复用 `compile_when`）；
/// - 安全护栏：route/run 目标命中安全步 → 报错（不可触发）；pre 边界终止/
///   转移跳过安全步、post 转移跳越区间含安全步 → 报错（不可跳过；反向转移
///   与 run 动作不跳步）。
pub fn compile_pipeline_with_checks(
    config: &PipelineConfig,
    step_library: &StepLibrary,
    plugin_ids: &HashSet<String>,
    index: Option<&StepServiceIndex>,
    body_checks: &[(String, Vec<StepCheck>)],
    safety_locked_steps: &[String],
) -> Result<CompiledPipeline, CompileError> {
    let mut compiled = compile_pipeline_with_index(config, step_library, plugin_ids, index)?;
    compiled.checks = compile_checks(body_checks, safety_locked_steps, &compiled)?;
    Ok(compiled)
}

/// 编译 checks 声明为分发表（词表解析 + 存在性校验 + 安全护栏执法）。
fn compile_checks(
    body_checks: &[(String, Vec<StepCheck>)],
    safety_locked_steps: &[String],
    compiled: &CompiledPipeline,
) -> Result<Vec<CompiledCheck>, CompileError> {
    let mut out = Vec::new();
    for (body_id, checks) in body_checks {
        let Some(body) = compiled.bodies.iter().find(|b| &b.id == body_id) else {
            return Err(CompileError {
                location: "checks 声明".to_string(),
                message: format!("checks 宿主循环体 '{body_id}' 不是已声明的循环体"),
            });
        };
        let step_pos = |id: &str| body.steps.iter().position(|s| s.id == *id);
        let safety: HashSet<&str> = safety_locked_steps.iter().map(|s| s.as_str()).collect();
        for (i, decl) in checks.iter().enumerate() {
            let location = format!("循环体 '{body_id}' / check #{}", i + 1);
            let trigger = compile_check_trigger(&decl.at, &step_pos, &location)?;
            let when = compile_when(decl.when.as_deref(), &location)?;
            let action = compile_check_action(&decl.then, &step_pos, &location)?;
            enforce_check_safety(
                &trigger,
                &action,
                &step_pos,
                body.steps.len(),
                &safety,
                &location,
            )?;
            out.push(CompiledCheck {
                body_id: body_id.clone(),
                trigger,
                when,
                action,
            });
        }
    }
    Ok(out)
}

/// 解析触发时机（`pre:<步>` / `post:<步>` / `state:<键>`；触发步存在性校验）。
fn compile_check_trigger(
    at: &str,
    step_pos: &impl Fn(&str) -> Option<usize>,
    location: &str,
) -> Result<CheckTrigger, CompileError> {
    let err = |message: String| CompileError {
        location: location.to_string(),
        message,
    };
    let Some((kind, rest)) = at.split_once(':') else {
        return Err(err(format!(
            "触发时机 '{at}' 非法（合法形态：pre:<step id> / post:<step id> / state:<键>）"
        )));
    };
    match kind {
        "pre" | "post" => {
            if rest.is_empty() {
                return Err(err(format!("触发时机 '{at}' 缺 step id")));
            }
            if step_pos(rest).is_none() {
                return Err(err(format!(
                    "触发时机 '{at}' 的步 '{rest}' 不在本循环体 steps 中"
                )));
            }
            Ok(if kind == "pre" {
                CheckTrigger::Pre(rest.to_string())
            } else {
                CheckTrigger::Post(rest.to_string())
            })
        }
        "state" => {
            if rest.is_empty() || rest.contains(':') {
                return Err(err(format!(
                    "触发时机 '{at}' 非法：state 订阅键必须是单个非空顶层键（不含冒号）"
                )));
            }
            Ok(CheckTrigger::State(rest.to_string()))
        }
        other => Err(err(format!(
            "触发时机 '{at}' 未知（'{other}' 不是 pre/post/state）"
        ))),
    }
}

/// 解析动作（`route:<步>` / `terminate` / `run:<步>`；目标存在性校验）。
fn compile_check_action(
    then: &str,
    step_pos: &impl Fn(&str) -> Option<usize>,
    location: &str,
) -> Result<CheckAction, CompileError> {
    let err = |message: String| CompileError {
        location: location.to_string(),
        message,
    };
    if then == "terminate" {
        return Ok(CheckAction::Terminate);
    }
    let Some((kind, target)) = then.split_once(':') else {
        return Err(err(format!(
            "动作 '{then}' 非法（合法词表：route:<step id> / terminate / run:<step id>）"
        )));
    };
    if target.is_empty() {
        return Err(err(format!("动作 '{then}' 缺目标 step id")));
    }
    if step_pos(target).is_none() {
        return Err(err(format!(
            "动作 '{then}' 的目标步 '{target}' 不在本循环体 steps 中"
        )));
    }
    match kind {
        "route" => Ok(CheckAction::Route(target.to_string())),
        "run" => Ok(CheckAction::Run(target.to_string())),
        other => Err(err(format!(
            "动作 '{then}' 未知（'{other}' 不是 route/run）"
        ))),
    }
}

/// 安全护栏执法（编译期，设计 §4.3：安全关键步骤不可被 checks 跳过或触发）。
///
/// - **不可触发**：route/run 目标命中安全步 → 报错；
/// - **不可跳过**：按（触发边界, 动作）静态计算被跳过的步位区间，与安全步
///   位置相交即报错——pre:X 转移/终止使 X 不执行（区间 [X..目标) / [X..末]）；
///   post:X 转移跳越 (X,目标) 开区间、终止跳过 (X,末]；反向转移不跳过区间
///   （目标之前的步后续仍会执行）；run 动作只追加执行不跳步；
///   state 触发无源位置（运行时任意写点触发），只受"不可触发"约束。
fn enforce_check_safety(
    trigger: &CheckTrigger,
    action: &CheckAction,
    step_pos: &impl Fn(&str) -> Option<usize>,
    body_len: usize,
    safety: &HashSet<&str>,
    location: &str,
) -> Result<(), CompileError> {
    let err = |message: String| CompileError {
        location: location.to_string(),
        message,
    };
    // 不可触发：动作目标不得是安全步
    if let CheckAction::Route(target) | CheckAction::Run(target) = action {
        if safety.contains(target.as_str()) {
            return Err(err(format!(
                "安全关键步骤 '{target}' 不可被 checks 触发（动作 '{}'）；\
                 安全步骤只能由管道流顺序执行或步产出路由到达",
                match action {
                    CheckAction::Route(_) => "route",
                    _ => "run",
                }
            )));
        }
    }
    // 不可跳过：静态计算被跳过的步位区间 [start, end)（半开；空区间 = 无跳过）
    let skipped: Option<(usize, usize)> = match (trigger, action) {
        (CheckTrigger::Pre(id), CheckAction::Route(target)) => {
            let (x, y) = (step_pos(id), step_pos(target));
            match (x, y) {
                (Some(x), Some(y)) if y > x => Some((x, y)), // X 被 divert 未执行
                (Some(x), _) => Some((x, x + 1)),            // 反向转移：仅 X 自身被跳过
                _ => None,
            }
        }
        (CheckTrigger::Pre(id), CheckAction::Terminate) => {
            step_pos(id).map(|x| (x, body_len)) // [X..末]
        }
        (CheckTrigger::Post(id), CheckAction::Route(target)) => {
            let (x, y) = (step_pos(id), step_pos(target));
            match (x, y) {
                (Some(x), Some(y)) if y > x => Some((x + 1, y)), // (X, 目标) 开区间
                _ => None,                                       // 反向/同位：无跳过
            }
        }
        (CheckTrigger::Post(id), CheckAction::Terminate) => {
            step_pos(id).map(|x| (x + 1, body_len)) // (X, 末]
        }
        _ => None, // Run 不跳步；State 触发无源位置
    };
    if let Some((start, end)) = skipped {
        let hit = safety
            .iter()
            .find(|s| step_pos(s).is_some_and(|p| p >= start && p < end));
        if let Some(hit_id) = hit {
            return Err(err(format!(
                "checks 动作会跳过安全关键步骤（被跳区间含安全步 '{hit_id}'）——\
                 安全步骤不可被 checks 跳过，请调整触发边界或动作目标"
            )));
        }
    }
    Ok(())
}

/// 编译管道 + hooks 装载表 + 步骤服务接线（hooks 机制退役后的兼容入口）。
///
/// **hooks 机制已退役**（钩子收敛终局，设计 §4.3：hook 不再独立存在，统一
/// 模型 = checks 分发 + run 步）。生产编译入口（api `load_and_compile_by_name`）
/// 恒传空 hooks（`to_internal` 已丢弃 YAML hooks 字段）——空入参行为零改动
/// （编译通过、checks 空表）；**非空 hooks 声明 = 编译错误**（fail-visible，
/// 文案含 checks 迁移指引，禁静默丢弃）。新声明一律走
/// [`compile_pipeline_with_checks`]。
pub fn compile_pipeline_with_hooks(
    config: &PipelineConfig,
    step_library: &StepLibrary,
    plugin_ids: &HashSet<String>,
    index: Option<&StepServiceIndex>,
    body_hooks: &[(String, Vec<HookFile>)],
    step_hooks: &[(String, Vec<HookFile>)],
) -> Result<CompiledPipeline, CompileError> {
    if !body_hooks.is_empty() || !step_hooks.is_empty() {
        return Err(CompileError {
            location: "hooks 声明".to_string(),
            message: "hooks 机制已退役（checks 统一机制取代）：请把 {on, run} 声明迁移为 \
                      checks 声明（如 step 级 {on: step_end, run} → {at: \"post:<step>\", \
                      then: \"run:<步骤id>\"}，经 compile_pipeline_with_checks 编译）"
                .to_string(),
        });
    }
    compile_pipeline_with_index(config, step_library, plugin_ids, index)
}

/// 编译后的循环体。
#[derive(Debug, Clone)]
pub struct CompiledBody {
    pub id: String,
    /// 是否循环（`while_cond` 存在即循环模式）。
    pub looping: bool,
    /// 循环继续条件（G10 DSL `while`；None = 无条件）。
    pub while_cond: Option<Expr>,
    /// 本循环体直接定义的步骤（已编译）。
    pub steps: Vec<CompiledStep>,
    /// 本循环体内 step id → 下标（Step 跳转用，编译期建好）。
    step_index: HashMap<String, usize>,
    /// 循环体结束后的转移（原 exit_routes / body 级 next，已编译）。
    pub exit_routes: Vec<CompiledRoute>,
    pub run_on_error: bool,
}

impl CompiledBody {
    /// 按 id 查本循环体内 step 下标（Step 跳转；编译期保证存在）。
    pub fn step_index(&self, id: &str) -> Option<usize> {
        self.step_index.get(id).copied()
    }
}

/// 编译后的步骤。
#[derive(Debug, Clone)]
pub struct CompiledStep {
    pub id: String,
    /// 组级 when 门（G9；None = 无条件执行）。
    pub when: Option<Expr>,
    /// 列表项（引用已解析）。
    pub items: Vec<CompiledItem>,
    /// 上下文注入（模板原文保留，运行时渲染——动态点）。
    pub context: HashMap<String, serde_json::Value>,
    /// 出口转移（已编译）。
    pub routes: Vec<CompiledRoute>,
    /// 步骤自带循环。
    pub loop_config: Option<LoopConfig>,
}

/// 编译后的列表项：引用已在加载期解析为三类之一。
#[derive(Debug, Clone)]
pub enum CompiledItem {
    /// 静态命中插件（三级命中③/四级命中④）。
    Plugin {
        plugin_id: String,
        /// 步骤服务解析结果（服务化提案 §3.2/§3.4）：`Some(步骤名)` = ③ 级命中
        /// 已注册步骤服务（运行期经 ctx 约定字段 `_step_method` 透传，SDK 侧
        /// 分发到对应注册函数）；`None` = ④ 级插件名直引（默认 execute 入口，
        /// legacy 语法糖，method None 不带键）。
        method: Option<String>,
        when: Option<Expr>,
        /// per-plugin inputs（经 config 通道传给插件，不 merge 进 state、不落 trace）。
        inputs: HashMap<String, serde_json::Value>,
    },
    /// 静态命中管道/库 step（三级命中①②），运行时查统一步骤池递归执行。
    Composite { step_id: String, when: Option<Expr> },
    /// 动态点：模板名（`{{state.xxx}}`），运行时渲染后再查池/插件（命中①②③）。
    Dynamic {
        template: String,
        when: Option<Expr>,
    },
}

impl CompiledItem {
    /// 项级 when 门（None = 无条件执行）。
    pub fn when(&self) -> Option<&Expr> {
        match self {
            CompiledItem::Plugin { when, .. }
            | CompiledItem::Composite { when, .. }
            | CompiledItem::Dynamic { when, .. } => when.as_ref(),
        }
    }
}

/// 编译后的转移分支。
#[derive(Debug, Clone)]
pub struct CompiledRoute {
    /// 条件 AST；None = 恒真（空/True 字面量归一）。
    pub when: Option<Expr>,
    /// 跳转目标（编译期已解析）。
    pub next: RouteNext,
    /// 命中时写入 state 的字段。
    pub set: HashMap<String, serde_json::Value>,
}

/// 编译管道（纯函数，无 IO）。
///
/// 未知引用（step 全 miss）、when 语法错误、引用环、转移目标不存在——全部在此
/// 报错。首个错误即返回（错误信息含定位，便于逐个修复）。
pub fn compile_pipeline(
    config: &PipelineConfig,
    step_library: &StepLibrary,
    plugin_ids: &HashSet<String>,
) -> Result<CompiledPipeline, CompileError> {
    let compiler = Compiler {
        plugin_ids,
        index: None,
        steps: Vec::new(),
        step_index: HashMap::new(),
        pool_ids: HashSet::new(),
    };
    compiler.compile(config, step_library)
}

/// 编译管道 + 步骤服务接线（checks/hooks 兼容入口内部复用的接线形态）。
fn compile_pipeline_with_index(
    config: &PipelineConfig,
    step_library: &StepLibrary,
    plugin_ids: &HashSet<String>,
    index: Option<&StepServiceIndex>,
) -> Result<CompiledPipeline, CompileError> {
    let compiler = Compiler {
        plugin_ids,
        index,
        steps: Vec::new(),
        step_index: HashMap::new(),
        pool_ids: HashSet::new(),
    };
    compiler.compile(config, step_library)
}

struct Compiler<'a> {
    plugin_ids: &'a HashSet<String>,
    /// 步骤服务索引（服务化提案 §3.2 第③级命中的数据层）。None = legacy 入口
    /// （[`compile_pipeline`]），插件名直引零改动；Some = 接线入口
    /// （[`compile_pipeline_with_hooks`]），③ 级命中携带 method、复合体隐式
    /// 诉求 fail-closed。
    index: Option<&'a StepServiceIndex>,
    steps: Vec<CompiledStep>,
    step_index: HashMap<String, usize>,
    /// 统一步骤池 id 集（Composite 判定用，owned 避免借用冲突）。
    pool_ids: HashSet<String>,
}

impl Compiler<'_> {
    fn compile(
        mut self,
        config: &PipelineConfig,
        step_library: &StepLibrary,
    ) -> Result<CompiledPipeline, CompileError> {
        // ── 第一遍：收集全部可引用 step（管道 step 优先，库 step 补缺）──
        // 统一步骤池的 id → 源（原始 PipelineStep 引用，编译在第二遍做）。
        let mut pool: Vec<(&str, &PipelineStep)> = Vec::new();
        let mut pool_index: HashMap<&str, usize> = HashMap::new();
        for body in &config.loop_bodies {
            for step in &body.steps {
                if pool_index.insert(step.id.as_str(), pool.len()).is_some() {
                    return Err(CompileError {
                        location: format!("循环体 '{}'", body.id),
                        message: format!("step id '{}' 重复（跨循环体）", step.id),
                    });
                }
                pool.push((step.id.as_str(), step));
            }
        }
        for (id, step) in &step_library.steps {
            if !pool_index.contains_key(id.as_str()) {
                pool_index.insert(id.as_str(), pool.len());
                pool.push((id.as_str(), step));
            }
        }

        // ── 第二遍：引用图环检测（composite 引用集 DFS）──
        // 引用图：pool 内 step id → 其 items 引用的 pool 内 step id 集（模板名/插件名不算）。
        let mut graph: HashMap<usize, Vec<usize>> = HashMap::new();
        for (i, (_id, step)) in pool.iter().enumerate() {
            let mut refs = Vec::new();
            for item in &step.steps {
                if is_dynamic_template(item.name()) {
                    continue; // 动态点运行时才定，不参与静态环检测
                }
                if let Some(&j) = pool_index.get(item.name()) {
                    refs.push(j); // 自引用也算边（DFS InStack 立即判环）
                }
            }
            graph.insert(i, refs);
        }
        let cycle = detect_cycle(&graph, pool.len());
        if let Some(node) = cycle {
            return Err(CompileError {
                location: "step 引用图".to_string(),
                message: format!(
                    "composite 引用环：step '{}' 递归引用自身（直接或间接）",
                    pool[node].0
                ),
            });
        }

        // ── 第三遍：编译 pool → 统一步骤池 ──
        // 先占位（id 只登记），再逐个编译（Composite 引用只需 id，池在编译完成后完整）。
        let n = pool.len();
        let mut compiled: Vec<CompiledStep> = Vec::with_capacity(n);
        // 占位空节点，保证 self.step_index 在编译引用期间可查（环已排除，不会自引用未编译）
        for _ in 0..n {
            compiled.push(CompiledStep {
                id: String::new(),
                when: None,
                items: Vec::new(),
                context: HashMap::new(),
                routes: Vec::new(),
                loop_config: None,
            });
        }
        // pool_ids：Composite 判定用（owned key 集）
        self.pool_ids = pool.iter().map(|(id, _)| (*id).to_string()).collect();
        for (i, (id, step)) in pool.iter().enumerate() {
            compiled[i] = self.compile_step(id, step)?;
        }
        // 登记 index（与 compiled 同步）
        for (i, (id, _)) in pool.iter().enumerate() {
            self.step_index.insert((*id).to_string(), i);
        }
        self.steps = compiled;

        // ── 第四遍：编译各循环体 ──
        let mut bodies = Vec::with_capacity(config.loop_bodies.len());
        for body in &config.loop_bodies {
            bodies.push(self.compile_body(body)?);
        }

        // ── 第五遍：转移目标校验 ──
        // Phase：全量 routes（统一步骤池 step + 循环体 step + 循环体 exit_routes）
        //        ——运行期 next_phase/exit_routes 都经 body_index 查找，目标须是
        //        已声明循环体 id。
        // Step：限循环体直接 steps 的 routes——运行期 execute_steps 只在本循环体
        //        steps 内查找跳转目标；统一步骤池（库 step）的 Step 路由运行期
        //        不消费（composite 执行丢弃返回值），不在此校验。
        let phase_ids: HashSet<&str> = config.loop_bodies.iter().map(|b| b.id.as_str()).collect();
        for step in &self.steps {
            check_phase_targets(&step.routes, &phase_ids, &format!("step '{}'", step.id))?;
        }
        for body in &bodies {
            let body_loc = format!("循环体 '{}'", body.id);
            check_phase_targets(&body.exit_routes, &phase_ids, &body_loc)?;
            for step in &body.steps {
                let step_loc = format!("{body_loc} / step '{}'", step.id);
                check_phase_targets(&step.routes, &phase_ids, &step_loc)?;
                for (i, route) in step.routes.iter().enumerate() {
                    if let RouteNext::Step(target) = &route.next {
                        if body.step_index(target).is_none() {
                            return Err(CompileError {
                                location: format!("{step_loc} / 转移 #{}", i + 1),
                                message: format!("Step 跳转目标 '{target}' 不在本循环体 steps 中"),
                            });
                        }
                    }
                }
            }
        }

        Ok(CompiledPipeline {
            name: config.name.clone(),
            bodies,
            checkpoint: config.checkpoint.clone(),
            config_hash: pipeline_config_hash(config),
            checks: Vec::new(),
            initial_state: config
                .initial_state
                .iter()
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect(),
            max_rounds: config.max_rounds,
            steps: self.steps,
            step_index: self.step_index,
        })
    }

    /// 编译单个 step（pool 内任意 step，含库 step）。
    fn compile_step(&self, id: &str, step: &PipelineStep) -> Result<CompiledStep, CompileError> {
        let location = format!("step '{id}'");
        let when = compile_when(step.when.as_deref(), &location)?;
        let mut items = Vec::with_capacity(step.steps.len());
        for item in &step.steps {
            let item_location = format!("{location} / 项 '{}'", item.name());
            let item_when = compile_when(item.when(), &item_location)?;
            if is_dynamic_template(item.name()) {
                items.push(CompiledItem::Dynamic {
                    template: item.name().to_string(),
                    when: item_when,
                });
            } else if self.pool_ids.contains(item.name()) {
                items.push(CompiledItem::Composite {
                    step_id: item.name().to_string(),
                    when: item_when,
                });
            } else if let Some(r) = self.index.and_then(|index| index.resolve(item.name())) {
                // ③ 级命中：已注册步骤服务（服务化提案 §3.2）——step 名 → 静态
                // 绑定 {plugin_id, method}。含单入口插件的隐式默认注册（name=插件
                // id，method None = 默认 execute 入口）与显式注册（method Some）。
                items.push(CompiledItem::Plugin {
                    plugin_id: r.plugin_id.clone(),
                    method: r.method.clone(),
                    when: item_when,
                    inputs: item.inputs(),
                });
            } else if self.plugin_ids.contains(item.name()) {
                // ④ 级插件名直引（legacy 语法糖）：索引存在时落到此处即 resolve
                // 落空——单入口纯步骤插件恒被隐式注册（resolve 必命中），故落空
                // 必为复合体（tools/services/steps 多能力）→ 编译期报错
                // （fail-closed 强制自白，提案 §3.3）；无索引（legacy 入口）保持
                // 现状零改动（method None = 默认 execute）。
                if self.index.is_some() {
                    return Err(CompileError {
                        location: item_location,
                        message: format!(
                            "步骤 '{}' 引用的插件 '{}' 为复合体（声明了 capabilities.tools/services/steps），\
                             未在其 capabilities.steps 中显式声明该步骤名——复合体须显式声明 \
                             capabilities.steps（含本步骤名）才能被管道步骤引用，禁止隐式默认",
                            step.id,
                            item.name()
                        ),
                    });
                }
                items.push(CompiledItem::Plugin {
                    plugin_id: item.name().to_string(),
                    method: None,
                    when: item_when,
                    inputs: item.inputs(),
                });
            } else {
                return Err(CompileError {
                    location: item_location,
                    message: format!(
                        "引用 '{}' 未找到（不是管道/库 step，也不是已加载插件）",
                        item.name()
                    ),
                });
            }
        }
        let routes = compile_routes(&step.routes, &location)?;
        if let Some(lc) = &step.loop_config {
            Self::validate_loop_config(lc, &format!("{location} / loop_config"))?;
        }
        Ok(CompiledStep {
            id: id.to_string(),
            when,
            items,
            context: step.context.clone(),
            routes,
            loop_config: step.loop_config.clone(),
        })
    }

    /// loop_config 校验（编译期 fail-closed；并行 for-each 原语，内容移植自
    /// sync/open-main-20261008 c2db32c26）：over/as 成对且为合法形态、
    /// max_concurrency 取值域、for-each 专属字段不出现在重复循环上、归并键为
    /// 单个顶层键名、迭代变量名不与引擎保留键冲突。
    fn validate_loop_config(lc: &LoopConfig, location: &str) -> Result<(), CompileError> {
        let err = |message: String| CompileError {
            location: location.to_string(),
            message,
        };
        if lc.over.is_some() != lc.as_iter.is_some() {
            return Err(err(
                "over 与 as 必须成对声明（for-each 循环缺一不可）".into()
            ));
        }
        if lc.over.is_none()
            && (!lc.collect.is_empty()
                || !lc.collect_append.is_empty()
                || lc.consume
                || !lc.consume_keys.is_empty())
        {
            return Err(err(
                "collect/collect_append/consume/consume_keys 仅 for-each（over）循环支持".into(),
            ));
        }
        if lc.max_concurrency == 0 || lc.max_concurrency < -1 {
            return Err(err(format!(
                "max_concurrency={} 非法（0 与 <-1 已禁用；1=串行链式，>1=有界并行，-1=不限）",
                lc.max_concurrency
            )));
        }
        if let Some(over) = &lc.over {
            let Some(src_key) = over.strip_prefix("state.") else {
                return Err(err(format!(
                    "over='{over}' 非法：必须是 'state.<顶层键>' 字面形式（非模板渲染）"
                )));
            };
            if src_key.is_empty() || src_key.contains('.') || src_key.contains('{') {
                return Err(err(format!(
                    "over='{over}' 非法：键必须是单个顶层键（不含点号/花括号）"
                )));
            }
            let as_name = lc.as_iter.as_deref().unwrap_or_default();
            if as_name.is_empty() || as_name.contains('.') {
                return Err(err(format!(
                    "as='{as_name}' 非法：迭代变量名必须是非空、不含点号的顶层键名"
                )));
            }
            const RESERVED: [&str; 5] = [
                "messages",
                "ended",
                "suspended",
                "next_phase",
                "current_phase",
            ];
            if RESERVED.contains(&as_name) {
                return Err(err(format!("as='{as_name}' 与引擎保留键冲突，请换名")));
            }
        }
        for key in lc
            .collect
            .iter()
            .chain(&lc.collect_append)
            .chain(&lc.consume_keys)
        {
            if key.is_empty() || key.contains('.') {
                return Err(err(format!(
                    "归并键 '{key}' 非法：必须是单个顶层键名（不含点号）"
                )));
            }
        }
        Ok(())
    }

    /// 编译循环体。
    fn compile_body(&self, body: &LoopBody) -> Result<CompiledBody, CompileError> {
        let location = format!("循环体 '{}'", body.id);
        // while 条件不能走 compile_when 的 True→None 归一：while 语义里
        // None = 未声明循环（单次执行），恒真循环必须保留字面量使 looping 成立。
        // 空串/字面量 True → Literal(true)（恒真循环，靠 ended/suspended 退出）。
        let while_cond = match body.while_cond.as_deref() {
            None => None,
            Some(c) => match parse_condition(c) {
                Ok(Some(Expr::Literal(v))) if v.as_bool() == Some(true) => {
                    Some(Expr::Literal(serde_json::Value::Bool(true)))
                }
                Ok(Some(other)) => Some(other),
                Ok(None) => Some(Expr::Literal(serde_json::Value::Bool(true))),
                Err(msg) => {
                    return Err(CompileError {
                        location,
                        message: format!("while 表达式语法错误: {msg}"),
                    });
                }
            },
        };
        // G10 单轨：循环模式唯一入口 = while 条件存在（迭代上限由 stop_check 按
        // Agent 配置 max_iterations 兜底，不在管道 DSL 表达）
        let looping = while_cond.is_some();
        let mut steps = Vec::with_capacity(body.steps.len());
        let mut step_index = HashMap::new();
        for step in &body.steps {
            step_index.insert(step.id.clone(), steps.len());
            steps.push(self.compile_step(&step.id, step)?);
        }
        let exit_routes = compile_routes(&body.exit_routes, &location)?;
        Ok(CompiledBody {
            id: body.id.clone(),
            looping,
            while_cond,
            steps,
            step_index,
            exit_routes,
            run_on_error: body.run_on_error,
        })
    }
}

/// 模板名判定：含 `{{` 的引用是运行时动态点（如 `{{state.core_plugin}}`）。
fn is_dynamic_template(name: &str) -> bool {
    name.contains("{{")
}

/// 编译 when 表达式：空/True 归一为 None（恒真），语法错误在此暴露。
fn compile_when(cond: Option<&str>, location: &str) -> Result<Option<Expr>, CompileError> {
    let Some(cond) = cond else {
        return Ok(None);
    };
    match parse_condition(cond) {
        Ok(None) => Ok(None),
        Ok(Some(expr)) => match expr {
            // 字面量 True 归一为恒真（跳过求值）
            Expr::Literal(v) if v.as_bool() == Some(true) => Ok(None),
            other => Ok(Some(other)),
        },
        Err(msg) => Err(CompileError {
            location: location.to_string(),
            message: format!("when 表达式语法错误: {msg}"),
        }),
    }
}

/// 编译转移列表：when 预编译 + 原样搬运。
/// Phase/Step 转移目标存在性校验在 [`Compiler::compile`] 尾声统一做
/// （需要完整的 body id 集 / 循环体 step id 集，见第五遍）。
fn compile_routes(routes: &[Route], location: &str) -> Result<Vec<CompiledRoute>, CompileError> {
    let mut out = Vec::with_capacity(routes.len());
    for (i, route) in routes.iter().enumerate() {
        let route_loc = format!("{location} / 转移 #{}", i + 1);
        let when = compile_when(Some(&route.when), &route_loc)?;
        out.push(CompiledRoute {
            when,
            next: route.then.next.clone(),
            set: route.then.set.clone(),
        });
    }
    Ok(out)
}

/// Phase 转移目标校验：目标须在已声明循环体 id 集内（运行期 body_index 查找范围）。
fn check_phase_targets(
    routes: &[CompiledRoute],
    phase_ids: &HashSet<&str>,
    location: &str,
) -> Result<(), CompileError> {
    for (i, route) in routes.iter().enumerate() {
        if let RouteNext::Phase(target) = &route.next {
            if !phase_ids.contains(target.as_str()) {
                return Err(CompileError {
                    location: format!("{location} / 转移 #{}", i + 1),
                    message: format!("Phase 转移目标 '{target}' 不是已声明的循环体"),
                });
            }
        }
    }
    Ok(())
}

/// 管道配置指纹（runs.config_hash 用）。
///
/// 确定性序列化：先 `serde_json::to_value`（serde_json::Map 默认 BTree 实现，
/// 键序稳定——与结构体字段内 HashMap 的插入序无关），再取 SHA-256 前 16 hex。
/// 同配置必同哈希、内容不同的配置哈希不同；指纹用于 runs 表审计/诊断定位，
/// 不承担密码学承诺。
pub fn pipeline_config_hash(config: &PipelineConfig) -> String {
    // PipelineConfig 为纯数据 derive(Serialize)，to_value/to_string 不可能失败；
    // 若失败宁可 panic 也不能兜 Null/空串——那会让所有配置得到同一 config_hash，
    // 静默摧毁 runs 审计字段。
    let canonical =
        serde_json::to_value(config).expect("PipelineConfig serialization is infallible");
    let json =
        serde_json::to_string(&canonical).expect("serde_json Value serialization is infallible");
    let mut hasher = Sha256::new();
    hasher.update(json.as_bytes());
    let hex = format!("{:x}", hasher.finalize());
    hex.chars().take(16).collect()
}

/// 引用环检测：有向图（边 i→j 表示 step i 引用 step j），DFS 找环。
/// 返回环上任意节点下标（None = 无环）。
fn detect_cycle(graph: &HashMap<usize, Vec<usize>>, n: usize) -> Option<usize> {
    #[derive(Clone, Copy, PartialEq)]
    enum Mark {
        Unvisited,
        InStack,
        Done,
    }
    let mut marks = vec![Mark::Unvisited; n];
    fn dfs(node: usize, graph: &HashMap<usize, Vec<usize>>, marks: &mut [Mark]) -> Option<usize> {
        marks[node] = Mark::InStack;
        if let Some(neighbors) = graph.get(&node) {
            for &next in neighbors {
                match marks[next] {
                    Mark::InStack => return Some(node),
                    Mark::Unvisited => {
                        if let Some(cycle) = dfs(next, graph, marks) {
                            return Some(cycle);
                        }
                    }
                    Mark::Done => {}
                }
            }
        }
        marks[node] = Mark::Done;
        None
    }
    for start in 0..n {
        if marks[start] == Mark::Unvisited {
            if let Some(cycle) = dfs(start, graph, &mut marks) {
                return Some(cycle);
            }
        }
    }
    None
}

// ═════════════════════════════════════════════════════════════════
// 单元测试
// ═════════════════════════════════════════════════════════════════

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_core::traits::{
        HostType, ManifestCapabilities, PluginType, StepCapability, ToolCapability,
    };
    use agentos_core::types::{LoopBody, PipelineConfig, Route, RouteAction, StepItem};
    use serde_json::json;

    /// 构造测试 manifest（默认单入口纯步骤插件形状：无 tools/services/steps）。
    fn manifest(id: &str) -> PluginManifest {
        PluginManifest {
            force_include_tools: Vec::new(),
            state: None,
            id: id.to_string(),
            name: format!("Test {id}"),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Pipeline,
            language: "python".to_string(),
            host_type: HostType::Sidecar,
            host_group: None,
            entry: "server.py".to_string(),
            capabilities: Default::default(),
            requires_services: vec![],
            permissions: Default::default(),
            priority: 100,
            mcp: None,
            lifecycle: None,
            native: None,
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            restricted_capabilities: Vec::new(),
            enabled: None,
            activation: None,
            provides: None,
            visibility: Default::default(),
            persistent_fields: vec![],
            export_fields: vec![],
            aux_venvs: Vec::new(),
        }
    }

    /// 给 manifest 设置 capabilities（steps/tools/services）。
    fn with_caps(mut m: PluginManifest, caps: ManifestCapabilities) -> PluginManifest {
        m.capabilities = caps;
        m
    }

    fn steps_caps(steps: Vec<StepCapability>) -> ManifestCapabilities {
        ManifestCapabilities {
            steps,
            ..Default::default()
        }
    }

    fn tools_caps(tools: Vec<ToolCapability>) -> ManifestCapabilities {
        ManifestCapabilities {
            tools,
            ..Default::default()
        }
    }

    fn make_step(id: &str, items: Vec<StepItem>) -> PipelineStep {
        PipelineStep {
            id: id.into(),
            steps: items,
            when: None,
            context: HashMap::new(),
            routes: vec![],
            loop_config: None,
        }
    }

    fn single_body(name: &str, steps: Vec<PipelineStep>) -> PipelineConfig {
        PipelineConfig {
            name: name.into(),
            loop_bodies: vec![LoopBody {
                id: "main".into(),
                steps,
                while_cond: None,
                exit_routes: vec![],
                run_on_error: false,
            }],
            checkpoint: Default::default(),
            initial_state: std::collections::HashMap::new(),
            max_rounds: None,
        }
    }

    fn plugins(ids: &[&str]) -> HashSet<String> {
        ids.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn compiles_plugin_items_and_when() {
        let config = single_body(
            "p",
            vec![make_step("s", vec![StepItem::Bare("alpha".into())])],
        );
        let compiled =
            compile_pipeline(&config, &StepLibrary::default(), &plugins(&["alpha"])).expect("ok");
        let body = &compiled.bodies[0];
        assert_eq!(body.steps.len(), 1);
        match &body.steps[0].items[0] {
            CompiledItem::Plugin {
                plugin_id, when, ..
            } => {
                assert_eq!(plugin_id, "alpha");
                assert!(when.is_none());
            }
            other => panic!("expected Plugin, got {other:?}"),
        }
    }

    #[test]
    fn compiles_composite_via_step_pool() {
        let config = single_body(
            "p",
            vec![
                make_step("inner", vec![StepItem::Bare("alpha".into())]),
                make_step("outer", vec![StepItem::Bare("inner".into())]),
            ],
        );
        let compiled =
            compile_pipeline(&config, &StepLibrary::default(), &plugins(&["alpha"])).expect("ok");
        let outer = &compiled.bodies[0].steps[1];
        match &outer.items[0] {
            CompiledItem::Composite { step_id, .. } => {
                assert_eq!(step_id, "inner");
                // 池可查
                assert_eq!(compiled.find_step("inner").unwrap().id, "inner");
            }
            other => panic!("expected Composite, got {other:?}"),
        }
    }

    #[test]
    fn library_steps_fill_the_pool() {
        let config = single_body(
            "p",
            vec![make_step("s", vec![StepItem::Bare("lib_step".into())])],
        );
        let mut lib = StepLibrary::default();
        lib.steps.insert(
            "lib_step".into(),
            make_step("lib_step", vec![StepItem::Bare("alpha".into())]),
        );
        let compiled = compile_pipeline(&config, &lib, &plugins(&["alpha"])).expect("ok");
        assert!(compiled.find_step("lib_step").is_some(), "库 step 入池");
    }

    #[test]
    fn dynamic_template_item_is_preserved() {
        let config = single_body(
            "p",
            vec![make_step(
                "s",
                vec![StepItem::Bare("{{state.core_plugin}}".into())],
            )],
        );
        let compiled =
            compile_pipeline(&config, &StepLibrary::default(), &plugins(&[])).expect("ok");
        match &compiled.bodies[0].steps[0].items[0] {
            CompiledItem::Dynamic { template, .. } => {
                assert_eq!(template, "{{state.core_plugin}}")
            }
            other => panic!("expected Dynamic, got {other:?}"),
        }
    }

    #[test]
    fn unknown_reference_is_compile_error() {
        let config = single_body(
            "p",
            vec![make_step("s", vec![StepItem::Bare("ghost".into())])],
        );
        let err = compile_pipeline(&config, &StepLibrary::default(), &plugins(&[])).unwrap_err();
        assert!(err.message.contains("ghost"), "err: {err}");
        assert!(err.location.contains("s"), "err: {err}");
    }

    #[test]
    fn invalid_when_is_compile_error() {
        let mut step = make_step("s", vec![]);
        step.when = Some("this is ((( invalid".into());
        let config = single_body("p", vec![step]);
        let err = compile_pipeline(&config, &StepLibrary::default(), &plugins(&[])).unwrap_err();
        assert!(err.message.contains("when"), "err: {err}");
        assert!(err.message.contains("语法"), "err: {err}");
    }

    #[test]
    fn when_true_literal_normalizes_to_none() {
        let mut step = make_step(
            "s",
            vec![StepItem::Gated {
                name: "alpha".into(),
                when: Some("True".into()),
                inputs: HashMap::new(),
            }],
        );
        step.when = Some("True".into());
        let config = single_body("p", vec![step]);
        let compiled =
            compile_pipeline(&config, &StepLibrary::default(), &plugins(&["alpha"])).expect("ok");
        let s = &compiled.bodies[0].steps[0];
        assert!(s.when.is_none(), "组级 True 归一恒真");
        match &s.items[0] {
            CompiledItem::Plugin { when, .. } => assert!(when.is_none(), "项级 True 归一恒真"),
            other => panic!("expected Plugin, got {other:?}"),
        }
    }

    #[test]
    fn gated_item_inputs_are_carried_into_plugin_item() {
        // step 项的 inputs 编译进 CompiledItem::Plugin，供运行时经 config 通道传给插件。
        let config = single_body(
            "p",
            vec![make_step(
                "s",
                vec![StepItem::Gated {
                    name: "alpha".into(),
                    when: None,
                    inputs: HashMap::from([("k".into(), serde_json::json!("v"))]),
                }],
            )],
        );
        let compiled =
            compile_pipeline(&config, &StepLibrary::default(), &plugins(&["alpha"])).expect("ok");
        match &compiled.bodies[0].steps[0].items[0] {
            CompiledItem::Plugin {
                plugin_id,
                method,
                when,
                inputs,
            } => {
                assert_eq!(plugin_id, "alpha");
                assert!(method.is_none(), "legacy 入口不携带 method");
                assert!(when.is_none());
                assert_eq!(inputs.get("k"), Some(&serde_json::json!("v")));
            }
            other => panic!("expected Plugin, got {other:?}"),
        }
    }

    #[test]
    fn composite_cycle_is_compile_error() {
        let config = single_body(
            "p",
            vec![
                make_step("a", vec![StepItem::Bare("b".into())]),
                make_step("b", vec![StepItem::Bare("a".into())]),
            ],
        );
        let err = compile_pipeline(&config, &StepLibrary::default(), &plugins(&[])).unwrap_err();
        assert!(err.message.contains("环"), "err: {err}");
    }

    #[test]
    fn self_reference_is_compile_error() {
        let config = single_body("p", vec![make_step("a", vec![StepItem::Bare("a".into())])]);
        let err = compile_pipeline(&config, &StepLibrary::default(), &plugins(&[])).unwrap_err();
        assert!(err.message.contains("环"), "err: {err}");
    }

    #[test]
    fn while_condition_compiles_and_enables_loop() {
        let mut body = LoopBody {
            id: "main".into(),
            steps: vec![make_step("s", vec![StepItem::Bare("alpha".into())])],
            while_cond: Some("state.n < 3".into()),
            exit_routes: vec![],
            run_on_error: false,
        };
        let config = PipelineConfig {
            name: "p".into(),
            loop_bodies: vec![body.clone()],
            checkpoint: Default::default(),
            initial_state: std::collections::HashMap::new(),
            max_rounds: None,
        };
        let compiled =
            compile_pipeline(&config, &StepLibrary::default(), &plugins(&["alpha"])).expect("ok");
        let cb = &compiled.bodies[0];
        assert!(cb.looping, "while 存在即循环模式");
        assert!(cb.while_cond.is_some());
        // 缺省 body（无 loop_config 无 while）不循环
        body.while_cond = None;
        let config2 = PipelineConfig {
            name: "p".into(),
            loop_bodies: vec![body],
            checkpoint: Default::default(),
            initial_state: std::collections::HashMap::new(),
            max_rounds: None,
        };
        let compiled2 =
            compile_pipeline(&config2, &StepLibrary::default(), &plugins(&["alpha"])).expect("ok");
        assert!(!compiled2.bodies[0].looping);
    }

    #[test]
    fn step_index_built_for_jump_targets() {
        let config = single_body(
            "p",
            vec![
                make_step("pre", vec![StepItem::Bare("alpha".into())]),
                make_step("post", vec![StepItem::Bare("alpha".into())]),
            ],
        );
        let compiled =
            compile_pipeline(&config, &StepLibrary::default(), &plugins(&["alpha"])).expect("ok");
        let body = &compiled.bodies[0];
        assert_eq!(body.step_index("pre"), Some(0));
        assert_eq!(body.step_index("post"), Some(1));
        assert_eq!(body.step_index("missing"), None);
    }

    #[test]
    fn routes_compile_when_ast() {
        let mut step = make_step("s", vec![]);
        step.routes = vec![Route {
            when: "state.done == true".into(),
            then: RouteAction {
                next: RouteNext::End,
                set: HashMap::from([("k".into(), json!(1))]),
            },
        }];
        let config = single_body("p", vec![step]);
        let compiled =
            compile_pipeline(&config, &StepLibrary::default(), &plugins(&[])).expect("ok");
        let r = &compiled.bodies[0].steps[0].routes[0];
        assert!(r.when.is_some(), "非恒真条件保留 AST");
        assert_eq!(r.next, RouteNext::End);
        assert_eq!(r.set.get("k"), Some(&json!(1)));
    }

    #[test]
    fn pipeline_step_precedes_library_on_name_clash() {
        let config = single_body(
            "p",
            vec![make_step("dup", vec![StepItem::Bare("alpha".into())])],
        );
        let mut lib = StepLibrary::default();
        lib.steps.insert(
            "dup".into(),
            make_step("dup", vec![StepItem::Bare("beta".into())]),
        );
        let compiled = compile_pipeline(&config, &lib, &plugins(&["alpha", "beta"])).expect("ok");
        // 管道 step 优先入池：库 dup 被跳过，alpha 才在池里
        assert!(compiled.find_step("dup").is_some());
        assert!(compiled.find_step("alpha").is_none());
    }

    // ── 转移目标校验（第五遍）──

    #[test]
    fn bad_step_route_phase_target_is_compile_error() {
        let mut step = make_step("s", vec![]);
        step.routes = vec![Route {
            when: "True".into(),
            then: RouteAction {
                next: RouteNext::Phase("ghost_body".into()),
                set: HashMap::new(),
            },
        }];
        let config = single_body("p", vec![step]);
        let err = compile_pipeline(&config, &StepLibrary::default(), &plugins(&[])).unwrap_err();
        assert!(err.message.contains("ghost_body"), "err: {err}");
        assert!(err.message.contains("Phase"), "err: {err}");
    }

    #[test]
    fn bad_exit_route_phase_target_is_compile_error() {
        let mut body = LoopBody {
            id: "main".into(),
            steps: vec![make_step("s", vec![StepItem::Bare("alpha".into())])],
            while_cond: None,
            exit_routes: vec![Route {
                when: "True".into(),
                then: RouteAction {
                    next: RouteNext::Phase("ghost_body".into()),
                    set: HashMap::new(),
                },
            }],
            run_on_error: false,
        };
        let config = PipelineConfig {
            name: "p".into(),
            loop_bodies: vec![body.clone()],
            checkpoint: Default::default(),
            initial_state: std::collections::HashMap::new(),
            max_rounds: None,
        };
        let err =
            compile_pipeline(&config, &StepLibrary::default(), &plugins(&["alpha"])).unwrap_err();
        assert!(err.message.contains("ghost_body"), "err: {err}");
        assert!(err.location.contains("main"), "err: {err}");
        // 目标改成存在的循环体 → 通过
        body.exit_routes[0].then.next = RouteNext::Phase("main".into());
        let config2 = PipelineConfig {
            name: "p".into(),
            loop_bodies: vec![body],
            checkpoint: Default::default(),
            initial_state: std::collections::HashMap::new(),
            max_rounds: None,
        };
        assert!(compile_pipeline(&config2, &StepLibrary::default(), &plugins(&["alpha"])).is_ok());
    }

    #[test]
    fn bad_step_jump_target_is_compile_error() {
        let mut step = make_step("s", vec![]);
        step.routes = vec![Route {
            when: "True".into(),
            then: RouteAction {
                next: RouteNext::Step("ghost_step".into()),
                set: HashMap::new(),
            },
        }];
        let config = single_body("p", vec![step]);
        let err = compile_pipeline(&config, &StepLibrary::default(), &plugins(&[])).unwrap_err();
        assert!(err.message.contains("ghost_step"), "err: {err}");
        assert!(err.message.contains("Step"), "err: {err}");
        assert!(err.location.contains("main"), "定位应含循环体：{err}");
    }

    #[test]
    fn valid_jump_and_phase_targets_compile() {
        // Step 目标在本循环体内 + Phase 目标是已声明循环体 → 编译通过
        let mut jumper = make_step("j", vec![]);
        jumper.routes = vec![
            Route {
                when: "True".into(),
                then: RouteAction {
                    next: RouteNext::Step("t".into()),
                    set: HashMap::new(),
                },
            },
            Route {
                when: "False".into(),
                then: RouteAction {
                    next: RouteNext::Phase("fallback".into()),
                    set: HashMap::new(),
                },
            },
        ];
        let config = PipelineConfig {
            name: "p".into(),
            loop_bodies: vec![
                LoopBody {
                    id: "main".into(),
                    steps: vec![jumper, make_step("t", vec![])],
                    while_cond: None,
                    exit_routes: vec![],
                    run_on_error: false,
                },
                LoopBody {
                    id: "fallback".into(),
                    steps: vec![],
                    while_cond: None,
                    exit_routes: vec![],
                    run_on_error: false,
                },
            ],
            checkpoint: Default::default(),
            initial_state: std::collections::HashMap::new(),
            max_rounds: None,
        };
        assert!(compile_pipeline(&config, &StepLibrary::default(), &plugins(&[])).is_ok());
    }

    // ── config_hash（A18）──

    #[test]
    fn config_hash_nonempty_stable_and_sensitive() {
        let config = single_body(
            "p",
            vec![make_step("s", vec![StepItem::Bare("alpha".into())])],
        );
        let same = single_body(
            "p",
            vec![make_step("s", vec![StepItem::Bare("alpha".into())])],
        );
        let other = single_body(
            "p2",
            vec![make_step("s", vec![StepItem::Bare("alpha".into())])],
        );
        let h1 = pipeline_config_hash(&config);
        let h2 = pipeline_config_hash(&same);
        let h3 = pipeline_config_hash(&other);
        assert_eq!(h1.len(), 16, "SHA-256 前 16 hex：{h1}");
        assert!(h1.chars().all(|c| c.is_ascii_hexdigit()), "hex：{h1}");
        assert_eq!(h1, h2, "同配置同哈希");
        assert_ne!(h1, h3, "异配置异哈希");
        // 编译产物携带同一指纹
        let compiled =
            compile_pipeline(&config, &StepLibrary::default(), &plugins(&["alpha"])).expect("ok");
        assert_eq!(compiled.config_hash, h1);
    }

    #[test]
    fn config_hash_stable_across_hashmap_insertion_order() {
        // 路由 set 是 HashMap——不同插入序（同逻辑内容）不得影响指纹。
        let mk = |first: &str| {
            let mut set = HashMap::new();
            // 故意按不同顺序插入同样的键值对
            if first == "a" {
                set.insert("a".to_string(), json!(1));
                set.insert("b".to_string(), json!(2));
                set.insert("c".to_string(), json!(3));
            } else {
                set.insert("c".to_string(), json!(3));
                set.insert("b".to_string(), json!(2));
                set.insert("a".to_string(), json!(1));
            }
            let mut step = make_step("s", vec![]);
            step.routes = vec![Route {
                when: "True".into(),
                then: RouteAction {
                    next: RouteNext::End,
                    set,
                },
            }];
            single_body("p", vec![step])
        };
        let h1 = pipeline_config_hash(&mk("a"));
        let h2 = pipeline_config_hash(&mk("c"));
        assert_eq!(h1, h2, "HashMap 插入序不得影响指纹");
    }

    // ── 步骤服务索引（服务化提案 §3.2/§3.3）──

    #[test]
    fn step_index_explicit_entries_resolve() {
        let caps = steps_caps(vec![
            StepCapability {
                name: "task.inject_params".into(),
                description: Some("注入任务参数".into()),
                input_schema: None,
            },
            StepCapability {
                name: "task.remind".into(),
                description: None,
                input_schema: None,
            },
        ]);
        let index =
            build_step_service_index(&[with_caps(manifest("task_service"), caps)]).expect("ok");
        let r = index.resolve("task.inject_params").expect("命中");
        assert_eq!(r.plugin_id, "task_service");
        // method 存步骤 name 本身（SDK 侧按 name 分发，不发明 type 字段）
        assert_eq!(r.method.as_deref(), Some("task.inject_params"));
        let r2 = index.resolve("task.remind").expect("命中");
        assert_eq!(r2.plugin_id, "task_service");
        assert_eq!(r2.method.as_deref(), Some("task.remind"));
        assert!(index.resolve("ghost").is_none());
        assert_eq!(index.names(), vec!["task.inject_params", "task.remind"]);
    }

    #[test]
    fn step_index_explicit_entry_named_as_plugin_id_maps_to_default_execute() {
        // 兼容映射（接线 ADR 2026-10-08-step-service-level3-wiring）：显式条目
        // name == 插件 id（现网 manifests 通用声明形态 `[{name: "pipeline_<id>"}]`）
        // → method None = 默认 execute 语义，与④插件名直引完全等价。
        let caps = steps_caps(vec![StepCapability {
            name: "pipeline_task_service".into(),
            description: None,
            input_schema: None,
        }]);
        let index = build_step_service_index(&[with_caps(manifest("pipeline_task_service"), caps)])
            .expect("ok");
        let r = index.resolve("pipeline_task_service").expect("命中");
        assert_eq!(r.plugin_id, "pipeline_task_service");
        assert_eq!(r.method, None);
    }

    #[test]
    fn step_index_implicit_default_for_single_entry_plugins() {
        // 单入口纯步骤插件（无 tools/services/steps）→ 隐式默认注册 name=插件 id
        let index = build_step_service_index(&[manifest("alpha"), manifest("beta")]).expect("ok");
        let r = index.resolve("alpha").expect("隐式注册");
        assert_eq!(r.plugin_id, "alpha");
        assert_eq!(r.method, None, "None = 默认 execute 入口");
        assert_eq!(index.resolve("beta").expect("隐式注册").plugin_id, "beta");
        assert_eq!(index.names(), vec!["alpha", "beta"]);
    }

    #[test]
    fn step_index_composite_without_explicit_steps_fails_closed() {
        // 声明了 tools 的复合体：隐式默认对其关闭（§3.3 fail-closed）——索引
        // 不为其伪造 {name: 插件id} 条目，管道按插件 id 直引 → resolve 落空 →
        // 编译期报错（强制自白条款）。索引构建本身不报错：纯工具插件
        // （tools 非空、永不当管道步骤）合法存在，不能因无 steps 而炸掉索引。
        let composite = with_caps(
            manifest("task_service"),
            tools_caps(vec![agentos_core::traits::ToolCapability {
                name: "task_submit".into(),
                description: None,
                input_schema: None,
                output_schema: None,
                category: None,
                ui: None,
                render: None,
                isolation_capable: None,
                smoke: None,
                timeout_ms: None,
            }]),
        );
        let index = build_step_service_index(&[composite]).expect("复合体不阻断索引构建");
        assert!(
            index.resolve("task_service").is_none(),
            "复合体无隐式默认条目（引用即编译错误）"
        );
        assert!(index.names().is_empty());
    }

    #[test]
    fn step_index_composite_with_explicit_steps_registers_them() {
        // 复合体显式声明 steps → 正常注册（多能力合一插件的正规形态）
        let composite = with_caps(
            manifest("task_service"),
            ManifestCapabilities {
                steps: vec![StepCapability {
                    name: "task.remind".into(),
                    description: None,
                    input_schema: None,
                }],
                tools: vec![agentos_core::traits::ToolCapability {
                    name: "task_submit".into(),
                    description: None,
                    input_schema: None,
                    output_schema: None,
                    category: None,
                    ui: None,
                    render: None,
                    isolation_capable: None,
                    smoke: None,
                    timeout_ms: None,
                }],
                ..Default::default()
            },
        );
        let index = build_step_service_index(&[composite]).expect("ok");
        let r = index.resolve("task.remind").expect("显式步骤注册");
        assert_eq!(r.plugin_id, "task_service");
        assert_eq!(r.method.as_deref(), Some("task.remind"));
        // 插件 id 本身不是步骤名（除非显式声明）
        assert!(index.resolve("task_service").is_none());
    }

    #[test]
    fn step_index_explicit_duplicate_names_conflict() {
        let a = with_caps(
            manifest("plugin_a"),
            steps_caps(vec![StepCapability {
                name: "task.remind".into(),
                description: None,
                input_schema: None,
            }]),
        );
        let b = with_caps(
            manifest("plugin_b"),
            steps_caps(vec![StepCapability {
                name: "task.remind".into(),
                description: None,
                input_schema: None,
            }]),
        );
        let err = build_step_service_index(&[a, b]).unwrap_err();
        assert!(err.message.contains("task.remind"), "err: {err}");
        assert!(err.message.contains("plugin_a"), "err: {err}");
        assert!(err.message.contains("plugin_b"), "err: {err}");
    }

    #[test]
    fn step_index_explicit_vs_implicit_conflict() {
        // 插件 A 显式声明 step 名 = 插件 B 的 id → 显式与隐式冲突
        let a = with_caps(
            manifest("plugin_a"),
            steps_caps(vec![StepCapability {
                name: "alpha".into(),
                description: None,
                input_schema: None,
            }]),
        );
        let b = manifest("alpha");
        let err = build_step_service_index(&[a, b]).unwrap_err();
        assert!(err.message.contains("alpha"), "err: {err}");
        assert!(err.message.contains("plugin_a"), "err: {err}");
        assert!(err.message.contains("alpha"), "err: {err}");
    }

    #[test]
    fn step_index_empty_manifests_yields_empty_index() {
        let index = build_step_service_index(&[]).expect("ok");
        assert!(index.resolve("anything").is_none());
        assert!(index.names().is_empty());
    }

    // ── checks 统一检测声明（钩子收敛终局，设计 §4.3）──

    use agentos_core::types::StepCheck;

    /// 构造单循环体 + checks 声明的编译入参（with_checks 入口）。
    fn compile_with_checks(
        config: &PipelineConfig,
        body_checks: &[(String, Vec<StepCheck>)],
        safety: &[&str],
    ) -> Result<CompiledPipeline, CompileError> {
        compile_pipeline_with_checks(
            config,
            &StepLibrary::default(),
            &plugins(&["alpha", "beta"]),
            None,
            body_checks,
            &safety.iter().map(|s| s.to_string()).collect::<Vec<_>>(),
        )
    }

    fn check(at: &str, when: Option<&str>, then: &str) -> StepCheck {
        StepCheck {
            at: at.to_string(),
            when: when.map(|s| s.to_string()),
            then: then.to_string(),
        }
    }

    /// 合法声明编译：触发时机/when AST/动作三词表逐项落位。
    #[test]
    fn checks_compile_triggers_when_and_actions() {
        let config = single_body(
            "p",
            vec![
                make_step("s1", vec![StepItem::Bare("alpha".into())]),
                make_step("s2", vec![StepItem::Bare("beta".into())]),
                make_step("watch", vec![]),
            ],
        );
        let compiled = compile_with_checks(
            &config,
            &[(
                "main".into(),
                vec![
                    check("pre:s1", None, "run:watch"),
                    check("post:s2", Some("flag == 'x'"), "terminate"),
                    check("state:flag", None, "route:s2"),
                ],
            )],
            &[],
        )
        .expect("合法 checks 编译通过");
        assert_eq!(compiled.checks.len(), 3);
        assert_eq!(compiled.checks[0].body_id, "main");
        assert_eq!(
            compiled.checks[0].trigger,
            CheckTrigger::Pre("s1".into()),
            "pre 触发解析"
        );
        assert!(compiled.checks[0].when.is_none(), "缺省 when 恒真");
        assert_eq!(compiled.checks[0].action, CheckAction::Run("watch".into()));
        assert_eq!(
            compiled.checks[1].trigger,
            CheckTrigger::Post("s2".into()),
            "post 触发解析"
        );
        assert!(compiled.checks[1].when.is_some(), "when 表达式编译为 AST");
        assert_eq!(compiled.checks[1].action, CheckAction::Terminate);
        assert_eq!(
            compiled.checks[2].trigger,
            CheckTrigger::State("flag".into()),
            "state 键级触发解析"
        );
        assert_eq!(compiled.checks[2].action, CheckAction::Route("s2".into()));
    }

    /// when 语法错误 → 编译错（AST 复用 compile_when，加载期暴露）。
    #[test]
    fn check_invalid_when_is_compile_error() {
        let config = single_body(
            "p",
            vec![make_step("s1", vec![StepItem::Bare("alpha".into())])],
        );
        for when in ["this is ((( invalid", "state.x >"] {
            let err = compile_with_checks(
                &config,
                &[(
                    "main".into(),
                    vec![check("post:s1", Some(when), "terminate")],
                )],
                &[],
            )
            .unwrap_err();
            assert!(err.message.contains("when"), "when={when} err: {err}");
            assert!(err.message.contains("语法"), "err: {err}");
            assert!(err.location.contains("main"), "定位含循环体: {err}");
        }
    }

    /// route/run 目标不存在 → 编译错（fail-closed，不静默丢弃）。
    #[test]
    fn check_target_missing_is_compile_error() {
        let config = single_body(
            "p",
            vec![make_step("s1", vec![StepItem::Bare("alpha".into())])],
        );
        for then in ["route:ghost", "run:ghost"] {
            let err = compile_with_checks(
                &config,
                &[("main".into(), vec![check("post:s1", None, then)])],
                &[],
            )
            .unwrap_err();
            assert!(
                err.message.contains("ghost"),
                "then={then} 错误须含目标名: {err}"
            );
            assert!(err.location.contains("main"), "err: {err}");
        }
    }

    /// 触发时机引用不存在的步 → 编译错。
    #[test]
    fn check_trigger_step_missing_is_compile_error() {
        let config = single_body(
            "p",
            vec![make_step("s1", vec![StepItem::Bare("alpha".into())])],
        );
        for at in ["pre:ghost", "post:ghost"] {
            let err = compile_with_checks(
                &config,
                &[("main".into(), vec![check(at, None, "terminate")])],
                &[],
            )
            .unwrap_err();
            assert!(err.message.contains("ghost"), "at={at} err: {err}");
        }
    }

    /// at/then 词表外形态 → 编译错（触发时机未知前缀 / 动作未知词）。
    #[test]
    fn check_malformed_at_and_then_are_compile_errors() {
        let config = single_body(
            "p",
            vec![make_step("s1", vec![StepItem::Bare("alpha".into())])],
        );
        let err = compile_with_checks(
            &config,
            &[(
                "main".into(),
                vec![check("midstream:s1", None, "terminate")],
            )],
            &[],
        )
        .unwrap_err();
        assert!(err.message.contains("midstream:s1"), "err: {err}");
        let err = compile_with_checks(
            &config,
            &[("main".into(), vec![check("post:s1", None, "explode:s1")])],
            &[],
        )
        .unwrap_err();
        assert!(err.message.contains("explode:s1"), "err: {err}");
        let err = compile_with_checks(
            &config,
            &[("main".into(), vec![check("state:", None, "terminate")])],
            &[],
        )
        .unwrap_err();
        assert!(err.message.contains("state:"), "空键名报错: {err}");
        // 未知循环体 id → 编译错
        let err = compile_with_checks(
            &config,
            &[("nobody".into(), vec![check("post:s1", None, "terminate")])],
            &[],
        )
        .unwrap_err();
        assert!(err.message.contains("nobody"), "err: {err}");
    }

    /// 护栏①：安全关键步骤不可被 checks 触发（route/run 目标命中即编译错）。
    #[test]
    fn check_cannot_trigger_safety_locked_step() {
        let config = single_body(
            "p",
            vec![
                make_step("s1", vec![StepItem::Bare("alpha".into())]),
                make_step("approval", vec![StepItem::Bare("beta".into())]),
            ],
        );
        for then in ["route:approval", "run:approval"] {
            let err = compile_with_checks(
                &config,
                &[("main".into(), vec![check("post:s1", None, then)])],
                &["approval"],
            )
            .unwrap_err();
            assert!(
                err.message.contains("approval"),
                "then={then} 错误须含安全步 id: {err}"
            );
            assert!(err.message.contains("安全"), "err: {err}");
        }
    }

    /// 护栏①：安全关键步骤不可被 checks 跳过——pre:X 终止/转移使 X 不再执行
    /// （X 安全步）→ 编译错；run 动作不跳步（X 照常执行）→ 合法。
    #[test]
    fn check_cannot_skip_safety_locked_step_from_pre() {
        let config = single_body(
            "p",
            vec![
                make_step("gate", vec![StepItem::Bare("alpha".into())]),
                make_step("fallback", vec![StepItem::Bare("beta".into())]),
            ],
        );
        let err = compile_with_checks(
            &config,
            &[("main".into(), vec![check("pre:gate", None, "terminate")])],
            &["gate"],
        )
        .unwrap_err();
        assert!(err.message.contains("gate"), "err: {err}");
        let err = compile_with_checks(
            &config,
            &[(
                "main".into(),
                vec![check("pre:gate", None, "route:fallback")],
            )],
            &["gate"],
        )
        .unwrap_err();
        assert!(err.message.contains("gate"), "pre 转移跳过安全步: {err}");
        // run 动作不改变 X 的执行（先跑 run 步再执行 gate）→ 合法
        assert!(
            compile_with_checks(
                &config,
                &[("main".into(), vec![check("pre:gate", None, "run:fallback")])],
                &["gate"],
            )
            .is_ok(),
            "pre:run 不跳过安全步"
        );
    }

    /// 护栏①：post:X route:Y 跳过 (X,Y) 区间中的安全步 → 编译错；目标本身
    /// 是安全步已由护栏①拦；区间无安全步 → 合法。
    #[test]
    fn check_route_skipping_safety_step_is_compile_error() {
        let config = single_body(
            "p",
            vec![
                make_step("s1", vec![StepItem::Bare("alpha".into())]),
                make_step("audit", vec![StepItem::Bare("beta".into())]),
                make_step("s3", vec![StepItem::Bare("alpha".into())]),
            ],
        );
        let err = compile_with_checks(
            &config,
            &[("main".into(), vec![check("post:s1", None, "route:s3")])],
            &["audit"],
        )
        .unwrap_err();
        assert!(err.message.contains("audit"), "跳过安全步须报错: {err}");
        // 反向跳转（s3 → s1）不越过 audit → 合法
        assert!(
            compile_with_checks(
                &config,
                &[("main".into(), vec![check("post:s3", None, "route:s1")])],
                &["audit"],
            )
            .is_ok(),
            "反向转移不跳过安全步"
        );
    }

    /// hooks 机制退役（收敛终局）：非空 hooks 声明 → 编译错（fail-visible，
    /// 文案含 checks 迁移指引）；空 hooks（生产恒传形态）零改动。
    #[test]
    fn legacy_hooks_declaration_is_retired_compile_error() {
        let config = single_body(
            "p",
            vec![make_step("s1", vec![StepItem::Bare("alpha".into())])],
        );
        let err = compile_pipeline_with_hooks(
            &config,
            &StepLibrary::default(),
            &plugins(&["alpha"]),
            None,
            &[(
                "main".into(),
                vec![HookFile {
                    on: "step_end".into(),
                    run: "watcher.on_step".into(),
                }],
            )],
            &[],
        )
        .unwrap_err();
        assert!(err.message.contains("退役"), "err: {err}");
        assert!(err.message.contains("checks"), "迁移指引: {err}");
        // step 级 hooks 同样退役
        let err = compile_pipeline_with_hooks(
            &config,
            &StepLibrary::default(),
            &plugins(&["alpha"]),
            None,
            &[],
            &[(
                "main:s1".into(),
                vec![HookFile {
                    on: "step_start".into(),
                    run: "watcher".into(),
                }],
            )],
        )
        .unwrap_err();
        assert!(err.message.contains("退役"), "err: {err}");
        // 空 hooks（api 生产恒传形态）→ 编译通过、checks 空表
        let compiled = compile_pipeline_with_hooks(
            &config,
            &StepLibrary::default(),
            &plugins(&["alpha"]),
            None,
            &[],
            &[],
        )
        .expect("空 hooks 编译通过");
        assert!(compiled.checks.is_empty(), "无声明 → checks 空表短路");
    }

    /// 空 checks 声明（未声明）→ 编译产物 checks 空表（分发点零开销短路）。
    #[test]
    fn no_checks_yields_empty_table() {
        let config = single_body(
            "p",
            vec![make_step("s", vec![StepItem::Bare("alpha".into())])],
        );
        let compiled =
            compile_pipeline(&config, &StepLibrary::default(), &plugins(&["alpha"])).expect("ok");
        assert!(compiled.checks.is_empty());
        assert!(compiled
            .checks_for_trigger("main", &CheckTrigger::Pre("s".into()))
            .is_empty());
    }

    // ── 四级命中接线（服务化提案 §3.2/§3.4：index 注入 → method 携带）──

    /// 带索引编译：具名步骤服务命中 → CompiledItem::Plugin 携带 method（步骤名）。
    #[test]
    fn indexed_compile_named_step_carries_method() {
        // 复合体显式声明 steps：step 名 ≠ 插件 id，③ 级命中静态绑定到插件 + method
        let composite = with_caps(
            manifest("task_service"),
            ManifestCapabilities {
                steps: vec![StepCapability {
                    name: "task.inject_params".into(),
                    description: None,
                    input_schema: None,
                }],
                tools: vec![agentos_core::traits::ToolCapability {
                    name: "task_submit".into(),
                    description: None,
                    input_schema: None,
                    output_schema: None,
                    category: None,
                    ui: None,
                    render: None,
                    isolation_capable: None,
                    smoke: None,
                    timeout_ms: None,
                }],
                ..Default::default()
            },
        );
        let index = build_step_service_index(&[composite]).expect("ok");
        let config = single_body(
            "p",
            vec![make_step(
                "s",
                vec![StepItem::Bare("task.inject_params".into())],
            )],
        );
        let compiled = compile_pipeline_with_hooks(
            &config,
            &StepLibrary::default(),
            &plugins(&["task_service"]),
            Some(&index),
            &[],
            &[],
        )
        .expect("具名步骤服务命中编译通过");
        match &compiled.bodies[0].steps[0].items[0] {
            CompiledItem::Plugin {
                plugin_id,
                method,
                when,
                ..
            } => {
                assert_eq!(plugin_id, "task_service");
                assert_eq!(method.as_deref(), Some("task.inject_params"));
                assert!(when.is_none());
            }
            other => panic!("expected Plugin, got {other:?}"),
        }
    }

    /// 带索引编译：单入口插件隐式默认注册（name=插件 id，method None）→
    /// ④ 级直引零报错、不携带 _step_method（默认 execute 入口）。
    #[test]
    fn indexed_compile_implicit_single_entry_plugin_no_method() {
        let index = build_step_service_index(&[manifest("alpha")]).expect("ok");
        let config = single_body(
            "p",
            vec![make_step("s", vec![StepItem::Bare("alpha".into())])],
        );
        let compiled = compile_pipeline_with_hooks(
            &config,
            &StepLibrary::default(),
            &plugins(&["alpha"]),
            Some(&index),
            &[],
            &[],
        )
        .expect("隐式单入口直引通过");
        match &compiled.bodies[0].steps[0].items[0] {
            CompiledItem::Plugin {
                plugin_id, method, ..
            } => {
                assert_eq!(plugin_id, "alpha");
                assert_eq!(method, &None, "隐式默认入口不携带 method");
            }
            other => panic!("expected Plugin, got {other:?}"),
        }
    }

    /// 带索引编译：复合体插件 id 直引（resolve 落空）→ 编译期报错（fail-closed
    /// 强制自白），文案含步骤名/插件 id/修复指引。
    #[test]
    fn indexed_compile_composite_direct_reference_fails_closed() {
        let composite = with_caps(
            manifest("task_service"),
            tools_caps(vec![agentos_core::traits::ToolCapability {
                name: "task_submit".into(),
                description: None,
                input_schema: None,
                output_schema: None,
                category: None,
                ui: None,
                render: None,
                isolation_capable: None,
                smoke: None,
                timeout_ms: None,
            }]),
        );
        let index = build_step_service_index(&[composite]).expect("复合体不阻断索引构建");
        let config = single_body(
            "p",
            vec![make_step("s", vec![StepItem::Bare("task_service".into())])],
        );
        let err = compile_pipeline_with_hooks(
            &config,
            &StepLibrary::default(),
            &plugins(&["task_service"]),
            Some(&index),
            &[],
            &[],
        )
        .expect_err("复合体直引必须编译期报错");
        assert!(err.message.contains("task_service"), "err: {err}");
        assert!(err.message.contains("capabilities.steps"), "err: {err}");
        assert!(err.message.contains("复合体"), "err: {err}");
    }

    /// legacy 入口（无索引）：插件名直引零改动——复合体也不报错（未接线，
    /// 语义与既有第③级 miss 宽容一致），method 恒 None。
    #[test]
    fn legacy_compile_without_index_is_unchanged() {
        // 复合体 manifest 只用于构建索引；legacy 路径不传索引 → 无 fail-closed
        let config = single_body(
            "p",
            vec![make_step("s", vec![StepItem::Bare("alpha".into())])],
        );
        let compiled =
            compile_pipeline(&config, &StepLibrary::default(), &plugins(&["alpha"])).expect("ok");
        match &compiled.bodies[0].steps[0].items[0] {
            CompiledItem::Plugin {
                plugin_id, method, ..
            } => {
                assert_eq!(plugin_id, "alpha");
                assert_eq!(method, &None, "legacy 直引恒 None");
            }
            other => panic!("expected Plugin, got {other:?}"),
        }
    }

    /// referenced_plugin_ids：Plugin 项 + Composite 池递归全收集；
    /// Dynamic 模板与未被引用的插件不进集合（预热集只含确定性引用）。
    /// checks 动作目标是循环体 step（其插件经步骤项已被收集），不新增引用。
    #[test]
    fn referenced_plugin_ids_covers_steps_and_composite() {
        let config = single_body(
            "p",
            vec![
                make_step("s1", vec![StepItem::Bare("alpha".into())]),
                make_step("outer", vec![StepItem::Bare("inner".into())]),
                make_step("s2", vec![StepItem::Bare("{{state.dyn}}".into())]),
            ],
        );
        // inner 为库 step（进统一步骤池），展开到 beta
        let mut lib = StepLibrary::default();
        lib.steps.insert(
            "inner".into(),
            make_step("inner", vec![StepItem::Bare("beta".into())]),
        );
        let plugin_ids = plugins(&["alpha", "beta", "gamma", "delta"]);
        let compiled = compile_pipeline(&config, &lib, &plugin_ids).expect("ok");

        let ids = compiled.referenced_plugin_ids();
        assert!(ids.contains("alpha"), "Plugin 项直引: {ids:?}");
        assert!(
            ids.contains("beta"),
            "Composite 池递归展开 inner→beta: {ids:?}"
        );
        // 性质断言：确定性引用集 ⊆ 编译传入插件集，且不含未引用插件/步骤 id
        assert!(ids.is_subset(&plugin_ids), "引用集须为插件 id: {ids:?}");
        assert!(!ids.contains("delta"), "未引用插件不进预热集: {ids:?}");
        assert!(
            !ids.contains("inner") && !ids.contains("outer") && !ids.contains("s1"),
            "步骤 id 不是插件 id（inner 若被当插件收集说明 Composite 未展开或误收 id）: {ids:?}"
        );
        // Dynamic 模板运行时才解析——不产生确定性引用（{{state.dyn}} 里 dyn 不该出现）
        assert!(!ids.contains("dyn"), "Dynamic 模板名不进集合: {ids:?}");
    }

    /// referenced_plugin_ids：空管道 + 无 hooks = 空集（空集短路性质）。
    #[test]
    fn referenced_plugin_ids_empty_pipeline_yields_empty() {
        let compiled = compile_pipeline(
            &single_body("p", vec![]),
            &StepLibrary::default(),
            &plugins(&["alpha"]),
        )
        .expect("ok");
        assert!(compiled.referenced_plugin_ids().is_empty());
    }
}
