//! # 管道加载期编译器（G10）
//!
//! 把 [`PipelineConfig`]（含公共 step 库）在**加载期一次性编译**成
//! [`CompiledPipeline`] 执行计划，运行时零解析、零三级命中重算：
//!
//! 1. **when 表达式预编译**：所有门/路由条件在编译期 tokenize + parse 成
//!    [`condition::Expr`] AST（`eval_condition` 每次调用都重 parse 的税被消灭）；
//!    语法错误在加载期报错（带 step/body 定位），不再"静默 false 死路由"。
//! 2. **三级命中静态解析**：step 列表项的引用在编译期判定为
//!    [`CompiledItem::Plugin`]（插件）/ [`CompiledItem::Composite`]（管道/库 step，
//!    运行时查统一步骤池递归）/ [`CompiledItem::Dynamic`]（`{{state.xxx}}` 模板名，
//!    运行时渲染后查池/插件——显式保留的动态点）；未知引用在加载期报错。
//! 3. **引用环检测**：composite 递归引用图 DFS，环 → 编译错误（运行时递归不死循环）。
//! 4. **转移目标校验**：`RouteNext::Phase/Step` 目标存在性在此复核（api 层
//!    `validate_no_name_conflicts` 已查 Phase，此处双保险 + Step 目标）。
//!
//! 编译是纯函数（`compile_pipeline`），无 IO 无时序，可同步单测。
//! 生产路径：启动期 / 热重载时编译一次 → `Arc<CompiledPipeline>` 原子换入，
//! 在途 run 持旧计划跑完（快照语义），新 run 取新计划。

use std::collections::{HashMap, HashSet};

use agentos_core::types::{
    CheckpointConfig, LoopBody, LoopConfig, PipelineConfig, PipelineStep, Route, RouteNext,
    StepLibrary,
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
    /// 静态命中插件（三级命中③）。
    Plugin {
        plugin_id: String,
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
        steps: Vec::new(),
        step_index: HashMap::new(),
        pool_ids: HashSet::new(),
    };
    compiler.compile(config, step_library)
}

struct Compiler<'a> {
    plugin_ids: &'a HashSet<String>,
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
            } else if self.plugin_ids.contains(item.name()) {
                items.push(CompiledItem::Plugin {
                    plugin_id: item.name().to_string(),
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
        Ok(CompiledStep {
            id: id.to_string(),
            when,
            items,
            context: step.context.clone(),
            routes,
            loop_config: step.loop_config.clone(),
        })
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
                    })
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
    use agentos_core::types::{LoopBody, PipelineConfig, Route, RouteAction, StepItem};
    use serde_json::json;

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
                when,
                inputs,
            } => {
                assert_eq!(plugin_id, "alpha");
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
}
