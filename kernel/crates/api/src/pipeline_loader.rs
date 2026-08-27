//! # 管道配置加载器 + 重名检测
//!
//! 启动期加载 0.2 引擎所需的配置：
//! - `config/pipelines/autonomous.yaml` → [`PipelineConfig`]
//! - `config/steps/*.yaml` → [`StepLibrary`]（每个文件是一个 `PipelineStep` 定义）
//!
//! 并提供 [`validate_no_name_conflicts`] 在启动期检测三类命名冲突：
//! ① pipeline.steps 的 id 之间不能重复；
//! ② pipeline.steps 的 id 不能与插件 id 冲突；
//! ③ step_library 的 id 不能与插件 id 冲突。
//!
//! 设计取舍（[来源: 任务 §pipeline_loader 实现要点]）：
//! - 文件缺失不报错：返回语义安全的默认（空配置 / 空 library），保证内核在
//!   缺省配置下仍可启动（降级为 echo）。
//! - 文件存在但解析失败：返回 `Err`（带上下文，方便定位）。
//! - 重名检测在 [`bin/agentos-kernel.rs`] 调用，冲突则 panic 退出。
//!
//! ## G10 统一 DSL（2026-08-15 定型，2026-08-25 单轨化）
//!
//! DSL 唯一形态（条件永远 `when`、目标永远 `then`、缺省顺序推进）：
//! - 转移写在 `next:` 列表：`- when: "expr"` + `then: <目标>`，目标为
//!   `end` / `loop` / step id（step 级）/ 循环体 id；`set:` 可附带 state 写入。
//! - 循环体循环条件：`while: "expr"`（存在即循环模式；迭代上限由 stop_check
//!   按 Agent 配置 max_iterations 兜底，不在管道 DSL 表达）。
//! - 旧形态（`loop_config:` / `routes:` / `exit_routes:` / `then: {next,set}`
//!   对象）已退役：`*File` 结构 deny_unknown_fields，旧键加载即报错。
//!
//! 解析分两层：`*File` 结构（YAML 直译，deny 未知键）→ 归一为内部
//! [`PipelineConfig`]（[`PipelineFile::to_internal`]：`next` 的 `then` 字符串在
//! 归一阶段解析为 [`RouteNext`]——body/step 目标全集在本文件内即可判定，未知目标
//! 在加载期报错，不静默）。

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use serde::Deserialize;

use agentos_core::types::{
    CheckpointConfig, LoopBody, LoopConfig, PipelineConfig, PipelineStep, Route, RouteAction,
    RouteNext, StepItem, StepLibrary,
};

// ═════════════════════════════════════════════════════════════════
// YAML 文件形态（G10：新旧 DSL 双形态解析层）
// ═════════════════════════════════════════════════════════════════

/// 转移分支：新 DSL 形态 `{when?, then: 目标字符串, set?}` 或旧形态
/// `{when, then: {next, set}}`（untagged 按声明顺序尝试，`then` 字符串 vs 对象天然区分）。
#[derive(Debug, Deserialize)]
#[serde(untagged)]
enum TransitionFile {
    /// 新 DSL：目标为字符串（end / loop / step id / body id），归一时解析。
    New {
        /// 条件表达式；缺省 None = 恒真（True）。
        #[serde(default)]
        when: Option<String>,
        /// 目标字符串。
        then: String,
        /// 命中时写入 state 的字段（可省）。
        #[serde(default)]
        set: HashMap<String, serde_json::Value>,
    },
}

impl TransitionFile {
    /// 归一为内部 [`Route`]。
    ///
    /// `then` 目标解析（新 DSL）：`end` / `loop` / 循环体 id（Phase）/ step id（Step，
    /// 仅 step 级转移，目标须在当前 body 的 step id 集内）。未知目标 → 语义错误。
    ///
    /// # Arguments
    /// - `body_ids`：全部循环体 id 集（Phase 目标判定）。
    /// - `local_step_ids`：当前 body 内 step id 集（Step 目标判定；None = 循环体级
    ///   转移，不接受 step 目标）。
    /// - `location`：定位串（如 `"step 'post'"`），错误信息用。
    fn into_route(
        self,
        body_ids: &HashSet<String>,
        local_step_ids: Option<&HashSet<String>>,
        location: &str,
    ) -> Result<Route, PipelineLoadError> {
        let (when, then, set) = match self {
            TransitionFile::New { when, then, set } => {
                let next = match then.as_str() {
                    "end" => RouteNext::End,
                    "loop" => RouteNext::Loop,
                    "wait" => {
                        return Err(PipelineLoadError::InvalidConfig(format!(
                            "{location}: 转移目标 'wait' 已不支持的 DSL（挂起由插件设 state.suspended 表达）"
                        )))
                    }
                    target => {
                        if let Some(local) = local_step_ids {
                            if local.contains(target) {
                                RouteNext::Step(target.to_string())
                            } else if body_ids.contains(target) {
                                RouteNext::Phase(target.to_string())
                            } else {
                                return Err(PipelineLoadError::InvalidConfig(format!(
                                    "{location}: 转移目标 '{target}' 未知（期望 end / loop / 本循环体内 step id / 循环体 id）"
                                )));
                            }
                        } else if body_ids.contains(target) {
                            RouteNext::Phase(target.to_string())
                        } else {
                            return Err(PipelineLoadError::InvalidConfig(format!(
                                "{location}: 循环体出口转移目标 '{target}' 未知（期望 end / 循环体 id）"
                            )));
                        }
                    }
                };
                (when.unwrap_or_else(|| "True".to_string()), next, set)
            }
        };
        Ok(Route {
            when,
            then: RouteAction { next: then, set },
        })
    }
}

/// 管道 YAML 文件形态（`*File` 结构直接对应 YAML 书写，归一后才进引擎类型）。
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PipelineFile {
    name: String,
    #[serde(default)]
    loop_bodies: Vec<LoopBodyFile>,
    #[serde(default)]
    checkpoint: CheckpointConfig,
}

impl PipelineFile {
    /// 归一为内部 [`PipelineConfig`]（`next`/`while` → `exit_routes`/`while_cond`）。
    #[allow(clippy::wrong_self_convention)] // 消费型转换（File 结构一次性归一到内部类型）
    fn to_internal(self) -> Result<PipelineConfig, PipelineLoadError> {
        self.to_internal_with_hooks().map(|(cfg, _)| cfg)
    }

    /// 归一为内部 [`PipelineConfig`] + hooks 声明（服务化提案 §3.6，单次解析）。
    ///
    /// hooks 不进入 [`PipelineConfig`]（引擎类型零改动）：body 级 hooks 按
    /// 循环体 id 收集，step 级 hooks 按 `"<body id>:<step id>"` 复合键收集，
    /// 供编译期 [`agentos_engine::compiler::compile_step_hooks`] 消费。
    #[allow(clippy::wrong_self_convention)] // 消费型转换（File 结构一次性归一到内部类型）
    fn to_internal_with_hooks(
        self,
    ) -> Result<(PipelineConfig, PipelineHooks), PipelineLoadError> {
        let body_ids: HashSet<String> = self.loop_bodies.iter().map(|b| b.id.clone()).collect();
        let mut loop_bodies = Vec::with_capacity(self.loop_bodies.len());
        let mut body_hooks = Vec::new();
        let mut step_hooks = Vec::new();
        for body in self.loop_bodies {
            let body_id = body.id.clone();
            let (body_internal, body_hooks_internal, step_hooks_internal) =
                body.to_internal_with_hooks(&body_ids)?;
            if !body_hooks_internal.is_empty() {
                body_hooks.push((body_id.clone(), body_hooks_internal));
            }
            loop_bodies.push(body_internal);
            for (step_id, hooks) in step_hooks_internal {
                step_hooks.push((format!("{body_id}:{step_id}"), hooks));
            }
        }
        Ok((
            PipelineConfig {
                name: self.name,
                loop_bodies,
                checkpoint: self.checkpoint,
            },
            PipelineHooks {
                body_hooks,
                step_hooks,
            },
        ))
    }
}

/// hooks 声明聚合（服务化提案 §3.6）：body 级 + step 级（复合键 `"<body id>:<step id>"`）。
///
/// 由 [`load_pipeline_with_hooks`] 单次解析产出，供编译期
/// [`agentos_engine::compiler::compile_step_hooks`] 消费；未声明 hooks 时两表皆空。
#[derive(Debug, Default)]
pub struct PipelineHooks {
    /// 管道级 hooks：`(循环体 id, 声明列表)`。
    pub body_hooks: Vec<(String, Vec<agentos_engine::compiler::HookFile>)>,
    /// step 级 hooks：`("<body id>:<step id>" 复合键, 声明列表)`。
    pub step_hooks: Vec<(String, Vec<agentos_engine::compiler::HookFile>)>,
}

/// 循环体 YAML 形态。
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct LoopBodyFile {
    id: String,
    #[serde(default)]
    steps: Vec<StepFile>,
    /// DSL：循环体循环继续条件（YAML 键 `while`）。
    #[serde(default, rename = "while")]
    while_cond: Option<String>,
    /// DSL：循环体出口转移（目标 = end / 循环体 id）。
    #[serde(default)]
    next: Vec<TransitionFile>,
    #[serde(default)]
    run_on_error: bool,
    /// 管道级 hooks（服务化提案 §3.6）：整个循环体生命周期的观察者。
    #[serde(default)]
    hooks: Vec<agentos_engine::compiler::HookFile>,
}

#[allow(clippy::wrong_self_convention)] // 消费型转换
impl LoopBodyFile {
    /// 归一为内部 [`LoopBody`] + body 级 hooks + step 级 hooks
    /// （`(step id, 声明列表)`）。
    #[allow(clippy::wrong_self_convention)] // 消费型转换
    fn to_internal_with_hooks(
        self,
        body_ids: &HashSet<String>,
    ) -> Result<
        (
            LoopBody,
            Vec<agentos_engine::compiler::HookFile>,
            Vec<(String, Vec<agentos_engine::compiler::HookFile>)>,
        ),
        PipelineLoadError,
    > {
        // step 级转移的 Step 目标判定需要本 body 的 step id 全集
        let local_step_ids: HashSet<String> = self.steps.iter().map(|s| s.id.clone()).collect();
        let location = format!("循环体 '{}'", self.id);

        // 循环体级转移：目标只接受 end / 循环体 id（step 级转移才接受 step id）
        let mut next_routes = Vec::with_capacity(self.next.len());
        for t in self.next {
            next_routes.push(t.into_route(body_ids, None, &location)?);
        }

        let mut steps = Vec::with_capacity(self.steps.len());
        let mut step_hooks = Vec::new();
        for step in self.steps {
            let (step_internal, hooks) =
                step.to_internal_with_hooks(body_ids, &local_step_ids, &location)?;
            if !hooks.is_empty() {
                step_hooks.push((step_internal.id.clone(), hooks));
            }
            steps.push(step_internal);
        }
        Ok((
            LoopBody {
                id: self.id,
                steps,
                while_cond: self.while_cond,
                exit_routes: next_routes,
                run_on_error: self.run_on_error,
            },
            self.hooks,
            step_hooks,
        ))
    }
}

/// step YAML 形态。
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct StepFile {
    id: String,
    #[serde(default)]
    steps: Vec<StepItem>,
    #[serde(default)]
    when: Option<String>,
    #[serde(default)]
    context: HashMap<String, serde_json::Value>,
    /// DSL：出口转移（目标 = end / loop / 本循环体内 step id / 循环体 id）。
    #[serde(default)]
    next: Vec<TransitionFile>,
    /// step 级循环（组合节点自带循环，如批量处理）。
    #[serde(default)]
    loop_config: Option<LoopConfig>,
    /// step 级 hooks（服务化提案 §3.6）：仅该 step 执行期间触发。
    #[serde(default)]
    hooks: Vec<agentos_engine::compiler::HookFile>,
}

#[allow(clippy::wrong_self_convention)] // 消费型转换
impl StepFile {
    fn to_internal(
        self,
        body_ids: &HashSet<String>,
        local_step_ids: &HashSet<String>,
        body_location: &str,
    ) -> Result<PipelineStep, PipelineLoadError> {
        self.to_internal_with_hooks(body_ids, local_step_ids, body_location)
            .map(|(s, _)| s)
    }

    /// 归一为内部 [`PipelineStep`] + step 级 hooks。
    #[allow(clippy::wrong_self_convention)] // 消费型转换
    fn to_internal_with_hooks(
        self,
        body_ids: &HashSet<String>,
        local_step_ids: &HashSet<String>,
        body_location: &str,
    ) -> Result<(PipelineStep, Vec<agentos_engine::compiler::HookFile>), PipelineLoadError> {
        let location = format!("{body_location} / step '{}'", self.id);
        let mut next_routes: Vec<Route> = Vec::with_capacity(self.next.len());
        for t in self.next {
            next_routes.push(t.into_route(body_ids, Some(local_step_ids), &location)?);
        }
        Ok((
            PipelineStep {
                id: self.id,
                steps: self.steps,
                when: self.when,
                context: self.context,
                routes: next_routes,
                loop_config: self.loop_config,
            },
            self.hooks,
        ))
    }
}

/// 加载管道配置（`config/pipelines/autonomous.yaml` → [`PipelineConfig`]）。
///
/// 文件不存在时返回默认配置（`loop.enabled=false`、空 `steps`），不报错——
/// 让内核在缺省配置下仍能启动（chat 走降级路径）。
///
/// 解析失败则返回 `Err`，错误信息含文件路径与 serde 错误细节。
pub fn load_pipeline_config(config_root: &Path) -> Result<PipelineConfig, PipelineLoadError> {
    let path = config_root.join("pipelines").join("autonomous.yaml");
    if !path.exists() {
        tracing::warn!(
            "Pipeline config not found at {}, using default (empty loop bodies)",
            path.display()
        );
        return Ok(PipelineConfig {
            name: "default".to_string(),
            loop_bodies: Vec::new(),
            checkpoint: Default::default(),
        });
    }
    let raw = std::fs::read_to_string(&path)
        .map_err(|e| PipelineLoadError::ReadFile(path.clone(), e.to_string()))?;
    let file: PipelineFile = serde_yaml::from_str(&raw)
        .map_err(|e| PipelineLoadError::ParseYaml(path.clone(), e.to_string()))?;
    let config = file.to_internal().map_err(|e| e.with_path(path.clone()))?;
    Ok(config)
}

/// 加载管道配置 + hooks 声明（服务化提案 §3.6，单次解析）。
///
/// 与 [`load_pipeline_config`] 同源（同一 YAML 文件、同一解析层），额外返回
/// hooks 聚合（body 级 + step 级复合键），供编译期
/// [`agentos_engine::compiler::compile_pipeline_with_hooks`] 消费。
/// 文件缺失时返回默认配置 + 空 hooks（与 [`load_pipeline_config`] 降级一致）。
pub fn load_pipeline_with_hooks(
    config_root: &Path,
) -> Result<(PipelineConfig, PipelineHooks), PipelineLoadError> {
    let path = config_root.join("pipelines").join("autonomous.yaml");
    if !path.exists() {
        tracing::warn!(
            "Pipeline config not found at {}, using default (empty loop bodies)",
            path.display()
        );
        return Ok((
            PipelineConfig {
                name: "default".to_string(),
                loop_bodies: Vec::new(),
                checkpoint: Default::default(),
            },
            PipelineHooks::default(),
        ));
    }
    let raw = std::fs::read_to_string(&path)
        .map_err(|e| PipelineLoadError::ReadFile(path.clone(), e.to_string()))?;
    let file: PipelineFile = serde_yaml::from_str(&raw)
        .map_err(|e| PipelineLoadError::ParseYaml(path.clone(), e.to_string()))?;
    let (config, hooks) = file
        .to_internal_with_hooks()
        .map_err(|e| e.with_path(path.clone()))?;
    Ok((config, hooks))
}

/// 加载公共 step 库（`config/steps/*.yaml` → [`StepLibrary`]）。
///
/// 每个 `*.yaml` 文件是单个 [`PipelineStep`] 定义（非数组），按文件 `id` 收录到
/// `StepLibrary.steps`。目录不存在返回空 library。
///
/// 同一 id 在多个文件出现时，**后加载覆盖先加载**（按文件名字典序），并在
/// warning 中提示——不视为致命错误，避免单文件冲突阻断整个内核启动。
pub fn load_step_library(config_root: &Path) -> Result<StepLibrary, PipelineLoadError> {
    let dir = config_root.join("steps");
    if !dir.exists() {
        tracing::warn!(
            "Step library dir not found at {}, using empty library",
            dir.display()
        );
        return Ok(StepLibrary::default());
    }

    // 收集 *.yaml 文件并按文件名稳定排序，保证不同平台/遍历顺序下结果一致
    let mut files: Vec<PathBuf> = Vec::new();
    for entry in std::fs::read_dir(&dir)
        .map_err(|e| PipelineLoadError::ReadDir(dir.clone(), e.to_string()))?
    {
        let entry = match entry {
            Ok(e) => e,
            Err(e) => {
                tracing::warn!("Skipping unreadable entry in {}: {}", dir.display(), e);
                continue;
            }
        };
        let p = entry.path();
        if p.is_file()
            && matches!(
                p.extension().and_then(|e| e.to_str()),
                Some("yaml") | Some("yml")
            )
        {
            files.push(p);
        }
    }
    files.sort();

    let mut library = StepLibrary::default();
    for path in files {
        let raw = match std::fs::read_to_string(&path) {
            Ok(s) => s,
            Err(e) => {
                tracing::warn!("Failed to read {}: {}, skipping", path.display(), e);
                continue;
            }
        };
        // 库文件走与管道 step 同源的 File 形态（DSL `next:` 才能解析）；库的
        // 消费方上下文未知，转移目标只接受 end/loop（step/循环体 id 目标无法
        // 在库加载期校验，fail-closed 报错，需要跨体转移请写在管道配置里）
        let file: StepFile = match serde_yaml::from_str(&raw) {
            Ok(s) => s,
            Err(e) => {
                // 解析失败：归并到 Err 列表（致命，让启动期暴露坏配置）
                return Err(PipelineLoadError::ParseYaml(path, e.to_string()));
            }
        };
        let empty: HashSet<String> = HashSet::new();
        let location = format!("公共 step 库 '{}'", file.id);
        let step: PipelineStep = file
            .to_internal(&empty, &empty, &location)
            .map_err(|e| e.with_path(path.clone()))?;
        if library.steps.contains_key(&step.id) {
            tracing::warn!(
                "Step id '{}' in {} already exists in library, overwriting (deduplication recommended)",
                step.id,
                path.display()
            );
        }
        library.steps.insert(step.id.clone(), step);
    }
    Ok(library)
}

/// 重名检测：在 pipeline 配置、公共 step 库、插件 id 集合三者间检测命名冲突。
///
/// 冲突类型（任一命中返回 `Err`，信息含具体冲突 id 与来源）：
/// ① 循环体 id 之间重复：`"循环体 id 'X' 重复（在 pipeline 配置中）"`
/// ② 全部循环体内的 step id 之间重复：`"step id 'X' 重复（在 pipeline 配置中）"`
/// ③ 全部循环体内的 step id 与插件 id 冲突：`"step id 'X' 与插件 id 冲突"`
/// ④ step_library 的 id 与插件 id 冲突：`"step id 'X' 与插件 id 冲突"`
/// ⑤ `RouteNext::Phase` 转移目标（step 级路由 / 循环体 exit_routes）指向
///    不存在的循环体：`"路由 Phase 目标 'X' 不存在（pipeline 配置）"`
///
/// 设计取舍：所有冲突一次性收集后返回第一条（首个报错即退出，避免错误信息噪声）。
pub fn validate_no_name_conflicts(
    pipeline: &PipelineConfig,
    step_library: &StepLibrary,
    plugin_ids: &HashSet<String>,
) -> Result<(), String> {
    // ① 循环体 id 之间不能重复
    let mut seen_body: HashSet<&str> = HashSet::new();
    for body in &pipeline.loop_bodies {
        if !seen_body.insert(body.id.as_str()) {
            return Err(format!(
                "循环体 id '{}' 重复（在 pipeline 配置中）",
                body.id
            ));
        }
    }

    // ② 全部循环体内的 step id 之间不能重复
    let mut seen: HashSet<&str> = HashSet::new();
    for step in pipeline.step_ids() {
        if !seen.insert(step) {
            return Err(format!("step id '{step}' 重复（在 pipeline 配置中）"));
        }
    }

    // ③ 全部循环体内的 step id 不能与插件 id 冲突
    for step in pipeline.step_ids() {
        if plugin_ids.contains(step) {
            return Err(format!("step id '{step}' 与插件 id 冲突（pipeline 配置）"));
        }
    }

    // ④ step_library id 不能与插件 id 冲突
    for id in step_library.steps.keys() {
        if plugin_ids.contains(id) {
            return Err(format!("step id '{id}' 与插件 id 冲突（公共 step 库）"));
        }
    }

    // ⑤ Phase 转移目标必须存在（step 级路由 + 循环体 exit_routes）
    let body_ids: HashSet<&str> = pipeline.loop_bodies.iter().map(|b| b.id.as_str()).collect();
    for step in pipeline.loop_bodies.iter().flat_map(|b| b.steps.iter()) {
        for route in &step.routes {
            if let RouteNext::Phase(id) = &route.then.next {
                if !body_ids.contains(id.as_str()) {
                    return Err(format!(
                        "路由 Phase 目标 '{id}' 不存在（pipeline 配置，step '{}'）",
                        step.id
                    ));
                }
            }
        }
    }
    for body in &pipeline.loop_bodies {
        for route in &body.exit_routes {
            if let RouteNext::Phase(id) = &route.then.next {
                if !body_ids.contains(id.as_str()) {
                    return Err(format!(
                        "exit_routes Phase 目标 '{id}' 不存在（pipeline 配置，循环体 '{}'）",
                        body.id
                    ));
                }
            }
        }
    }

    Ok(())
}

/// 配置加载错误（含文件路径 + 原因）。
#[derive(Debug)]
pub enum PipelineLoadError {
    /// 读取文件失败（路径 + 原因）
    ReadFile(PathBuf, String),
    /// 读取目录失败（路径 + 原因）
    ReadDir(PathBuf, String),
    /// YAML 解析失败（路径 + 原因）
    ParseYaml(PathBuf, String),
    /// 配置语义错误（如新 DSL 转移目标不存在；归一阶段产生，可携带路径）
    InvalidConfig(String),
}

impl PipelineLoadError {
    /// 把语义错误（无路径）绑定到触发文件路径。
    fn with_path(self, path: PathBuf) -> Self {
        match self {
            PipelineLoadError::InvalidConfig(msg) => {
                PipelineLoadError::InvalidConfig(format!("{}: {msg}", path.display()))
            }
            other => other,
        }
    }
}

impl std::fmt::Display for PipelineLoadError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PipelineLoadError::ReadFile(p, why) => {
                write!(f, "读取文件 {} 失败: {}", p.display(), why)
            }
            PipelineLoadError::ReadDir(p, why) => {
                write!(f, "读取目录 {} 失败: {}", p.display(), why)
            }
            PipelineLoadError::ParseYaml(p, why) => {
                write!(f, "解析 YAML {} 失败: {}", p.display(), why)
            }
            PipelineLoadError::InvalidConfig(msg) => write!(f, "配置语义错误: {msg}"),
        }
    }
}

impl std::error::Error for PipelineLoadError {}

// ═════════════════════════════════════════════════════════════════
// 单元测试
// ═════════════════════════════════════════════════════════════════

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_core::types::{LoopBody, Route, RouteAction};
    use std::collections::HashMap;
    use std::fs;
    use tempfile::TempDir;

    /// 在临时目录构造一份 autonomous.yaml，验证解析后的 PipelineConfig 关键字段。
    #[test]
    fn test_load_pipeline_config_reads_autonomous() {
        let tmp = TempDir::new().unwrap();
        let root = tmp.path();
        let yaml = r#"
name: test_pipeline
loop_bodies:
  - id: init
    steps:
      - id: setup
        steps:
          - env_resolver
  - id: main
    while: "True"
    steps:
      - id: prepare
        steps:
          - tool_schema
        context:
          agent_id: "A1"
"#;
        fs::create_dir_all(root.join("pipelines")).unwrap();
        fs::write(root.join("pipelines/autonomous.yaml"), yaml).unwrap();

        let cfg = load_pipeline_config(root).expect("should load");
        assert_eq!(cfg.name, "test_pipeline");
        assert_eq!(cfg.loop_bodies.len(), 2);
        assert_eq!(cfg.loop_bodies[0].id, "init");
        assert!(cfg.loop_bodies[0].while_cond.is_none());
        let main = &cfg.loop_bodies[1];
        assert_eq!(main.id, "main");
        assert_eq!(main.while_cond.as_deref(), Some("True"));
        assert_eq!(main.steps.len(), 1);
        assert_eq!(main.steps[0].id, "prepare");
        assert_eq!(
            main.steps[0].steps,
            vec![agentos_core::types::StepItem::Bare(
                "tool_schema".to_string()
            )]
        );
        assert_eq!(main.steps[0].context.get("agent_id").unwrap(), "A1");
    }

    /// G9：steps 列表项的 when 门 YAML 形态解析（裸串 + 对象两种写法共存）。
    #[test]
    fn test_step_item_when_gate_yaml_forms() {
        let yaml = r#"
name: gate_pipeline
loop_bodies:
  - id: main
    steps:
      - id: body
        when: "state.turn_count > 1"
        steps:
          - tool_schema
          - name: godot_context
            when: "state.selected != ''"
"#;
        let config: PipelineConfig = serde_yaml::from_str(yaml).unwrap();
        let body = &config.loop_bodies[0].steps[0];
        assert_eq!(
            body.when.as_deref(),
            Some("state.turn_count > 1"),
            "组级 when 门"
        );
        assert_eq!(body.steps.len(), 2);
        assert_eq!(body.steps[0].name(), "tool_schema");
        assert!(body.steps[0].when().is_none(), "裸串形态无门");
        assert_eq!(body.steps[1].name(), "godot_context");
        assert_eq!(
            body.steps[1].when(),
            Some("state.selected != ''"),
            "对象形态带门"
        );
    }

    /// 文件不存在 → 返回默认配置（不报错）。
    #[test]
    fn test_load_pipeline_config_missing_returns_default() {
        let tmp = TempDir::new().unwrap();
        let cfg = load_pipeline_config(tmp.path()).expect("missing config should not error");
        assert!(cfg.loop_bodies.is_empty());
    }

    /// hooks 声明解析（服务化提案 §3.6）：body 级 + step 级复合键 + 空配置零条目。
    #[test]
    fn test_load_pipeline_with_hooks_parses_two_level_scopes() {
        let tmp = TempDir::new().unwrap();
        let root = tmp.path();
        let yaml = r#"
name: hook_pipeline
loop_bodies:
  - id: main
    hooks:
      - on: stream_chunk
        run: watcher.on_chunk
    steps:
      - id: core
        hooks:
          - on: stream_chunk
            run: w2.on_chunk
      - id: plain
"#;
        fs::create_dir_all(root.join("pipelines")).unwrap();
        fs::write(root.join("pipelines/autonomous.yaml"), yaml).unwrap();

        let (cfg, hooks) = load_pipeline_with_hooks(root).expect("should load");
        assert_eq!(cfg.name, "hook_pipeline");
        assert_eq!(cfg.loop_bodies.len(), 1);
        // body 级 hooks：按循环体 id 收集
        assert_eq!(hooks.body_hooks.len(), 1);
        assert_eq!(hooks.body_hooks[0].0, "main");
        assert_eq!(hooks.body_hooks[0].1.len(), 1);
        assert_eq!(hooks.body_hooks[0].1[0].on, "stream_chunk");
        assert_eq!(hooks.body_hooks[0].1[0].run, "watcher.on_chunk");
        // step 级 hooks：复合键 "<body id>:<step id>"，未声明 hooks 的 step 不产生条目
        assert_eq!(hooks.step_hooks.len(), 1);
        assert_eq!(hooks.step_hooks[0].0, "main:core");
        assert_eq!(hooks.step_hooks[0].1[0].run, "w2.on_chunk");
    }

    /// hooks 空配置 → 两表皆空（发射点零开销短路）。
    #[test]
    fn test_load_pipeline_with_hooks_empty_yields_zero_entries() {
        let tmp = TempDir::new().unwrap();
        let root = tmp.path();
        let yaml = r#"
name: plain_pipeline
loop_bodies:
  - id: main
    steps:
      - id: core
"#;
        fs::create_dir_all(root.join("pipelines")).unwrap();
        fs::write(root.join("pipelines/autonomous.yaml"), yaml).unwrap();

        let (_cfg, hooks) = load_pipeline_with_hooks(root).expect("should load");
        assert!(hooks.body_hooks.is_empty());
        assert!(hooks.step_hooks.is_empty());
    }

    /// hooks 文件缺失 → 默认配置 + 空 hooks（与 load_pipeline_config 降级一致）。
    #[test]
    fn test_load_pipeline_with_hooks_missing_returns_default() {
        let tmp = TempDir::new().unwrap();
        let (cfg, hooks) = load_pipeline_with_hooks(tmp.path()).expect("missing should not error");
        assert!(cfg.loop_bodies.is_empty());
        assert!(hooks.body_hooks.is_empty());
        assert!(hooks.step_hooks.is_empty());
    }

    /// 坏 YAML → 返回 ParseYaml 错误（含路径）。
    #[test]
    fn test_load_pipeline_config_bad_yaml_errors() {
        let tmp = TempDir::new().unwrap();
        let root = tmp.path();
        fs::create_dir_all(root.join("pipelines")).unwrap();
        fs::write(
            root.join("pipelines/autonomous.yaml"),
            "name: x\n  bad: : :",
        )
        .unwrap();
        let err = load_pipeline_config(root).unwrap_err();
        assert!(matches!(err, PipelineLoadError::ParseYaml(..)));
    }

    /// 加载公共 step 库：两个 step 文件 → library 含两个条目。
    #[test]
    fn test_load_step_library_multiple_files() {
        let tmp = TempDir::new().unwrap();
        let root = tmp.path();
        let s1 = "id: step_a\nsteps:\n  - file_read\n";
        let s2 = "id: step_b\nsteps:\n  - llm_core\n";
        fs::create_dir_all(root.join("steps")).unwrap();
        fs::write(root.join("steps/a.yaml"), s1).unwrap();
        fs::write(root.join("steps/b.yaml"), s2).unwrap();

        let lib = load_step_library(root).expect("should load");
        assert!(lib.steps.contains_key("step_a"));
        assert!(lib.steps.contains_key("step_b"));
        assert_eq!(lib.steps.len(), 2);
    }

    /// 目录不存在 → 空 library。
    #[test]
    fn test_load_step_library_missing_returns_empty() {
        let tmp = TempDir::new().unwrap();
        let lib = load_step_library(tmp.path()).expect("missing dir should not error");
        assert!(lib.steps.is_empty());
    }

    /// 构造单循环体 pipeline（测试便捷函数）。
    fn single_body_pipeline(name: &str, steps: Vec<PipelineStep>) -> PipelineConfig {
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

    fn make_step(id: &str) -> PipelineStep {
        PipelineStep {
            id: id.into(),
            steps: vec![],
            when: None,
            context: HashMap::new(),
            routes: vec![],
            loop_config: None,
        }
    }

    /// validate_no_name_conflicts：无冲突 → Ok。
    #[test]
    fn test_validate_no_conflicts_ok() {
        let pipeline = single_body_pipeline("p", vec![make_step("s1")]);
        let mut lib = StepLibrary::default();
        lib.steps.insert("lib_a".into(), make_step("lib_a"));
        let mut plugin_ids = HashSet::new();
        plugin_ids.insert("plugin_x".to_string());
        assert!(validate_no_name_conflicts(&pipeline, &lib, &plugin_ids).is_ok());
    }

    /// 冲突①：循环体 id 重复。
    #[test]
    fn test_validate_conflict_duplicate_body_id() {
        let pipeline = PipelineConfig {
            name: "p".into(),
            loop_bodies: vec![
                LoopBody {
                    id: "main".into(),
                    steps: vec![],
                    while_cond: None,
                    exit_routes: vec![],
                    run_on_error: false,
                },
                LoopBody {
                    id: "main".into(),
                    steps: vec![],
                    while_cond: None,
                    exit_routes: vec![],
                    run_on_error: false,
                },
            ],
            checkpoint: Default::default(),
        };
        let lib = StepLibrary::default();
        let plugin_ids = HashSet::new();
        let err = validate_no_name_conflicts(&pipeline, &lib, &plugin_ids).unwrap_err();
        assert!(
            err.contains("main"),
            "err should name the conflicting id: {err}"
        );
        assert!(err.contains("循环体"));
    }

    /// 冲突②：pipeline step id 重复。
    #[test]
    fn test_validate_conflict_duplicate_pipeline_step_id() {
        let pipeline = single_body_pipeline("p", vec![make_step("dup"), make_step("dup")]);
        let lib = StepLibrary::default();
        let plugin_ids = HashSet::new();
        let err = validate_no_name_conflicts(&pipeline, &lib, &plugin_ids).unwrap_err();
        assert!(
            err.contains("dup"),
            "err should name the conflicting id: {err}"
        );
        assert!(err.contains("重复"));
    }

    /// 冲突③：pipeline step id 与插件 id 冲突。
    #[test]
    fn test_validate_conflict_pipeline_step_vs_plugin_id() {
        let pipeline = single_body_pipeline("p", vec![make_step("shared")]);
        let lib = StepLibrary::default();
        let mut plugin_ids = HashSet::new();
        plugin_ids.insert("shared".to_string());
        let err = validate_no_name_conflicts(&pipeline, &lib, &plugin_ids).unwrap_err();
        assert!(err.contains("shared"));
        assert!(err.contains("插件"));
    }

    /// 冲突④：step_library id 与插件 id 冲突。
    #[test]
    fn test_validate_conflict_library_step_vs_plugin_id() {
        let pipeline = single_body_pipeline("p", vec![]);
        let mut lib = StepLibrary::default();
        lib.steps
            .insert("doc_extract".into(), make_step("doc_extract"));
        let mut plugin_ids = HashSet::new();
        plugin_ids.insert("doc_extract".to_string());
        let err = validate_no_name_conflicts(&pipeline, &lib, &plugin_ids).unwrap_err();
        assert!(err.contains("doc_extract"));
        assert!(err.contains("插件"));
    }

    /// 冲突⑤：exit_routes 的 Phase 目标不存在 → 校验失败。
    #[test]
    fn test_validate_conflict_phase_target_missing() {
        let pipeline = PipelineConfig {
            name: "p".into(),
            loop_bodies: vec![LoopBody {
                id: "init".into(),
                steps: vec![],
                while_cond: None,
                exit_routes: vec![Route {
                    when: "True".into(),
                    then: RouteAction {
                        next: RouteNext::Phase("nonexistent".into()),
                        set: HashMap::new(),
                    },
                }],
                run_on_error: false,
            }],
            checkpoint: Default::default(),
        };
        let lib = StepLibrary::default();
        let plugin_ids = HashSet::new();
        let err = validate_no_name_conflicts(&pipeline, &lib, &plugin_ids).unwrap_err();
        assert!(err.contains("nonexistent"), "err: {err}");
        assert!(err.contains("Phase"));
    }

    /// G10 统一 DSL：next 形态解析——then 目标字符串归一为 RouteNext
    /// （end / loop / 本循环体内 step id / 循环体 id），while 归一为 while_cond。
    #[test]
    fn test_g10_next_dsl_forms() {
        let yaml = r#"
name: dsl_pipeline
loop_bodies:
  - id: main
    while: "state.turn < 5"
    steps:
      - id: core
        steps:
          - a
        next:
          - when: "core_type == 'tool_execute'"
            then: core
            set:
              core_type: llm_call
          - when: "raw_tool_calls != []"
            then: loop
          - then: end
  - id: exit
    run_on_error: true
    steps:
      - id: fin
        steps:
          - b
        next:
          - when: "True"
            then: main
"#;
        let config: PipelineConfig = serde_yaml::from_str::<PipelineFile>(yaml)
            .unwrap()
            .to_internal()
            .unwrap();
        let main = &config.loop_bodies[0];
        assert_eq!(
            main.while_cond.as_deref(),
            Some("state.turn < 5"),
            "while 归一"
        );
        let core = &main.steps[0];
        assert_eq!(core.routes.len(), 3, "next 三条全部归一为 routes");
        // then: core（本 body step id）→ Step
        assert_eq!(core.routes[0].then.next, RouteNext::Step("core".into()));
        assert_eq!(
            core.routes[0].then.set.get("core_type").unwrap(),
            "llm_call",
            "set 保留"
        );
        // then: loop → Loop（when 保留原文）
        assert_eq!(core.routes[1].then.next, RouteNext::Loop);
        assert_eq!(core.routes[1].when, "raw_tool_calls != []");
        // then: end + 缺省 when → True
        assert_eq!(core.routes[2].then.next, RouteNext::End);
        assert_eq!(core.routes[2].when, "True");
        // step 级 then: main（循环体 id，跨体）→ Phase
        assert_eq!(
            config.loop_bodies[1].steps[0].routes[0].then.next,
            RouteNext::Phase("main".into())
        );
    }

    /// G10 统一 DSL：循环体级 next 的目标只接受 end / 循环体 id。
    #[test]
    fn test_g10_body_next_phase_target() {
        let yaml = r#"
name: dsl_pipeline
loop_bodies:
  - id: init
    steps: []
    next:
      - when: "done == true"
        then: main
      - when: "True"
        then: end
  - id: main
    steps: []
"#;
        let config: PipelineConfig = serde_yaml::from_str::<PipelineFile>(yaml)
            .unwrap()
            .to_internal()
            .unwrap();
        let init = &config.loop_bodies[0];
        assert_eq!(init.exit_routes.len(), 2);
        assert_eq!(
            init.exit_routes[0].then.next,
            RouteNext::Phase("main".into())
        );
        assert_eq!(init.exit_routes[1].then.next, RouteNext::End);
    }

    /// G10 统一 DSL：未知 then 目标 → 加载期语义错误（不静默）。
    #[test]
    fn test_g10_unknown_then_target_errors() {
        let tmp = TempDir::new().unwrap();
        let root = tmp.path();
        fs::create_dir_all(root.join("pipelines")).unwrap();
        fs::write(
            root.join("pipelines/autonomous.yaml"),
            r#"
name: bad
loop_bodies:
  - id: main
    steps:
      - id: core
        steps: []
        next:
          - then: ghost_step
"#,
        )
        .unwrap();
        let err = load_pipeline_config(root).unwrap_err();
        assert!(
            matches!(err, PipelineLoadError::InvalidConfig(_)),
            "期望语义错误，got {err:?}"
        );
        let msg = err.to_string();
        assert!(msg.contains("ghost_step"), "错误应点名未知目标: {msg}");
        assert!(msg.contains("core"), "错误应带定位: {msg}");
    }

    /// G10 统一 DSL：循环体级 next 不接受 step id 目标。
    #[test]
    fn test_g10_body_next_rejects_step_target() {
        let yaml = r#"
name: bad
loop_bodies:
  - id: main
    steps:
      - id: core
        steps: []
    next:
      - then: core
"#;
        let err = serde_yaml::from_str::<PipelineFile>(yaml)
            .unwrap()
            .to_internal()
            .unwrap_err();
        assert!(matches!(err, PipelineLoadError::InvalidConfig(_)));
        assert!(err.to_string().contains("循环体出口转移"));
    }

    /// G10 端到端：仓库真实 config/pipelines/autonomous.yaml 用新 DSL 形态加载。
    /// （加载 + 归一成功即证明迁移后的 YAML 可被引擎消费；编译校验在启动期 fail-fast。）
    #[test]
    fn test_real_autonomous_yaml_loads_after_dsl_migration() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../..")
            .join("config");
        if !root.join("pipelines/autonomous.yaml").exists() {
            eprintln!("跳过：仓库 config 目录不存在（{root:?}）");
            return;
        }
        let cfg = load_pipeline_config(&root).expect("真实 autonomous.yaml 应能加载");
        assert_eq!(cfg.name, "autonomous");
        // 三个循环体：init / main / exit
        assert_eq!(
            cfg.loop_bodies.len(),
            3,
            "bodies: {:?}",
            cfg.loop_bodies.iter().map(|b| &b.id).collect::<Vec<_>>()
        );
        // main / post：DSL next 归一为 routes（6 条：对话挂起 + 对话结束 +
        // loop + loop + loop + end）
        let main = cfg
            .loop_bodies
            .iter()
            .find(|b| b.id == "main")
            .expect("main");
        // 单轨化后 main 循环模式唯一入口 = while
        assert_eq!(main.while_cond.as_deref(), Some("True"));
        let post = main
            .steps
            .iter()
            .find(|s| s.id == "post")
            .expect("post step");
        assert_eq!(post.routes.len(), 6, "post next 六条");
        // 对话模式分支（2026-08-27 接线 conversation_mode 时新增，置顶）
        assert_eq!(post.routes[0].then.next, RouteNext::Loop);
        assert_eq!(
            post.routes[0].when,
            "conversation_mode == True and raw_tool_calls == []"
        );
        assert_eq!(
            post.routes[0].then.set.get("suspended"),
            Some(&serde_json::json!(true)),
            "对话挂起经 set suspended=true 表达"
        );
        assert_eq!(
            post.routes[1].when,
            "conversation_mode == True and raw_tool_calls != []"
        );
        // 既有工具调用/回 LLM 分支顺序不变
        assert_eq!(
            post.routes[2].when,
            "raw_tool_calls != [] and raw_tool_calls != None"
        );
        assert_eq!(post.routes[5].then.next, RouteNext::End);
        assert_eq!(post.routes[5].when, "True", "缺省 when 归一为 True");
        // 动态 core_plugin 项保留（引擎动态点；tool_cache 接线后位于其前列）
        let core = main
            .steps
            .iter()
            .find(|s| s.id == "core")
            .expect("core step");
        assert!(
            core.steps.iter().any(|s| s.name() == "{{state.core_plugin}}"),
            "core 步骤应保留动态 core_plugin 项"
        );
        // steps 库可加载
        let lib = load_step_library(&root).expect("steps 库应能加载");
        assert!(
            lib.steps.contains_key("doc_extract") || lib.steps.is_empty(),
            "库加载不报错"
        );
    }

    /// 单轨化后旧形态 fail-closed：loop_config / exit_routes / routes /
    /// then 对象形态一律加载报错（deny_unknown_fields + then 只收字符串）。
    #[test]
    fn test_g10_legacy_forms_rejected() {
        let tmp = TempDir::new().unwrap();
        let root = tmp.path();
        fs::create_dir_all(root.join("pipelines")).unwrap();
        for (name, bad) in [
            (
                "loop_config",
                "name: p\nloop_bodies:\n  - id: main\n    loop_config:\n      enabled: true\n    steps: []\n",
            ),
            (
                "exit_routes",
                "name: p\nloop_bodies:\n  - id: main\n    exit_routes:\n      - when: \"True\"\n        then:\n          next: end\n    steps: []\n",
            ),
            (
                "routes",
                "name: p\nloop_bodies:\n  - id: main\n    steps:\n      - id: core\n        steps: []\n        routes:\n          - when: \"True\"\n            then:\n              next: loop\n",
            ),
            (
                "then_object",
                "name: p\nloop_bodies:\n  - id: main\n    steps:\n      - id: core\n        steps: []\n        next:\n          - when: \"True\"\n            then:\n              next: end\n",
            ),
        ] {
            fs::write(root.join("pipelines/autonomous.yaml"), bad).unwrap();
            let err = load_pipeline_config(root)
                .err()
                .unwrap_or_else(|| panic!("{name} 旧形态应加载报错"));
            assert!(
                matches!(err, PipelineLoadError::ParseYaml(_, _)),
                "{name}: 期望 ParseYaml，实际 {err:?}"
            );
        }
    }

    /// 端到端：用真实 autonomous.yaml + doc_extract.yaml 形态构造配置，
    /// 验证加载链不报错。
    #[test]
    fn test_load_real_config_shapes() {
        let tmp = TempDir::new().unwrap();
        let root = tmp.path();
        fs::create_dir_all(root.join("pipelines")).unwrap();
        fs::create_dir_all(root.join("steps")).unwrap();
        // autonomous.yaml（简化版，结构与 config/pipelines/autonomous.yaml 一致）
        fs::write(
            root.join("pipelines/autonomous.yaml"),
            r#"
name: autonomous
loop_bodies:
  - id: main
    while: "True"
    steps:
      - id: prepare
        steps:
          - tool_schema
        context:
          agent_id: "{{state.agent_id}}"
"#,
        )
        .unwrap();
        // doc_extract.yaml（公共 step 示例）
        fs::write(
            root.join("steps/doc_extract.yaml"),
            r#"
id: doc_extract
steps:
  - file_read
  - llm_core
context:
  task: "提取文档关键信息"
next:
  - when: "extract_result == ''"
    then: end
    set:
      status: failed
"#,
        )
        .unwrap();

        let cfg = load_pipeline_config(root).expect("pipeline config");
        assert_eq!(cfg.name, "autonomous");
        assert_eq!(cfg.loop_bodies.len(), 1);
        assert_eq!(cfg.loop_bodies[0].id, "main");
        assert_eq!(cfg.loop_bodies[0].while_cond.as_deref(), Some("True"));
        assert_eq!(cfg.loop_bodies[0].steps.len(), 1);
        assert_eq!(cfg.loop_bodies[0].steps[0].id, "prepare");

        let lib = load_step_library(root).expect("step library");
        let doc = lib
            .steps
            .get("doc_extract")
            .expect("库条目应加载（next: 形态）");
        assert_eq!(doc.routes.len(), 1);
        assert_eq!(doc.routes[0].then.next, RouteNext::End);

        let lib = load_step_library(root).expect("step library");
        assert!(lib.steps.contains_key("doc_extract"));

        let plugin_ids = HashSet::new();
        assert!(validate_no_name_conflicts(&cfg, &lib, &plugin_ids).is_ok());
    }
}
