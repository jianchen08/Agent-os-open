//! # 管道配置承载（P7）
//!
//! 内核 config crate 承载管道配置——插件执行 steps 列表（input 插件链 → 核心插件 →
//! output 插件链 → 路由仲裁 → 循环），数据格式以 0.1 真相源
//! `config/pipelines/default.yaml` 与 `config/agents/*.yaml` 为准。
//!
//! ## 模块组织
//!
//! - [`PipelineDefinition`]：0.1 扁平格式的结构化定义
//!   （name / task_worker / input_routes / output_routes / plugins / core_plugins），
//!   serde 可直接解析 `config/pipelines/*.yaml`。
//! - [`load_pipeline_definition`]：按名加载 `config/pipelines/{name}.yaml`。
//! - [`PipelineDefinition::to_engine_config`]：转换为引擎 steps 模型
//!   （`agentos_core::types::PipelineConfig`），由 `PipelineExecutor` 解释执行。
//! - [`AgentConfig`] / [`load_agent_config`]：加载 `config/agents/**/{id}.yaml`
//!   的关键字段（system_prompt / tool_ids / model_tier / max_iterations），
//!   供引擎注入管道 state。
//!
//! ## 转换设计（0.1 扁平格式 → 引擎 steps 模型）
//!
//! 0.1 语义：每条消息按 core_type 匹配 input_route（input 插件链）→ 执行 core
//! 插件 → output 插件链 → 路由仲裁决定下一步（tool/llm/wait/end）→ 循环。
//!
//! 引擎 steps 模型（0.2）：`prepare`（input 插件链）→ `core`（动态
//! `{{state.core_plugin}}`）→ `post`（output 插件链 + 路由仲裁），管道级
//! `loop_config.enabled=true` 循环直至 `ended`。
//!
//! 转换规则：
//! - `prepare.steps` = 所有 `target=core` 的 input_routes 插件并集（保序去重，
//!   插件名加 `pipeline_` 前缀，对齐 0.2 插件 id 约定）
//! - `core.steps` = `["{{state.core_plugin}}"]`（动态：llm_call 或 tool_execute）
//! - `post.steps` = plugins 中未被 input_routes 引用的 output 插件
//! - `post.routes` = output_routes 按 priority 排序转换的路由仲裁
//! - `loop_config.enabled = true`（0.1 默认循环，end 路由置 ended）
//!
//! [来源: docs/working/frontend_01_alignment_plan.md §P7]
//! [来源: config/pipelines/default.yaml]

use std::collections::HashSet;
use std::path::Path;

use serde::{Deserialize, Serialize};

use agentos_core::types::{LoopConfig, PipelineConfig, PipelineStep, Route, RouteAction, RouteNext};

use crate::error::ConfigError;

// ── 0.1 扁平格式结构定义 ─────────────────────────────────────────

/// 输入路由条目（0.1 `input_routes[].`）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InputRouteEntry {
    pub name: String,
    #[serde(default)]
    pub condition: String,
    #[serde(default = "default_target")]
    pub target: String,
    #[serde(default)]
    pub plugins: Vec<String>,
    #[serde(default)]
    pub priority: i32,
}

fn default_target() -> String {
    "core".to_string()
}

/// 输出路由条目（0.1 `output_routes[].`）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OutputRouteEntry {
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub route_type: String,
    #[serde(default)]
    pub condition: String,
    #[serde(default)]
    pub priority: i32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target_core: Option<String>,
    #[serde(default)]
    pub plugins: Vec<String>,
}

/// 插件配置（0.1 `plugins[].`）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PluginEntry {
    #[serde(default)]
    pub name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub class: Option<String>,
    #[serde(default)]
    pub config: serde_yaml::Value,
}

/// 核心插件配置（0.1 `core_plugins.{type}.`）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CorePluginEntry {
    #[serde(default)]
    pub class: String,
    #[serde(default)]
    pub config: serde_yaml::Value,
}

/// 管道定义（0.1 扁平格式，`config/pipelines/*.yaml` 真相源）。
///
/// 字段与 `config/pipelines/default.yaml` 一一对应：
/// name / task_worker / input_routes / output_routes / plugins / core_plugins。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PipelineDefinition {
    pub name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub task_worker: Option<serde_yaml::Value>,
    #[serde(default)]
    pub input_routes: Vec<InputRouteEntry>,
    #[serde(default)]
    pub output_routes: Vec<OutputRouteEntry>,
    #[serde(default)]
    pub plugins: Vec<PluginEntry>,
    #[serde(default)]
    pub core_plugins: std::collections::HashMap<String, CorePluginEntry>,
}

/// 按名加载管道定义（`config/pipelines/{name}.yaml`）。
///
/// # Errors
/// - [`ConfigError::NotFound`]：文件不存在。
/// - [`ConfigError::YamlParse`]：YAML 解析失败。
pub fn load_pipeline_definition(config_root: &Path, name: &str) -> Result<PipelineDefinition, ConfigError> {
    let path = config_root.join("pipelines").join(format!("{name}.yaml"));
    if !path.exists() {
        return Err(ConfigError::NotFound {
            path: path.to_string_lossy().to_string(),
        });
    }
    let content = std::fs::read_to_string(&path).map_err(|e| ConfigError::Io {
        message: format!("read {} failed: {e}", path.display()),
    })?;
    serde_yaml::from_str(&content).map_err(|e| ConfigError::YamlParse {
        path: path.to_string_lossy().to_string(),
        message: e.to_string(),
    })
}

// ── 引擎配置转换 ─────────────────────────────────────────────────

/// 0.2 引擎插件 id 前缀（对齐 `config/pipelines/autonomous.yaml` 与
/// `server.rs::DEFAULT_CORE_PLUGIN = "pipeline_llm_core"`）。
const PLUGIN_ID_PREFIX: &str = "pipeline_";

/// 插件短名 → 引擎插件 id（已带前缀则原样返回）。
fn to_plugin_id(name: &str) -> String {
    if name.starts_with(PLUGIN_ID_PREFIX) {
        name.to_string()
    } else {
        format!("{PLUGIN_ID_PREFIX}{name}")
    }
}

/// 核心插件类型 → 引擎插件 id。
///
/// 显式映射对齐 0.2 引擎既有事实（autonomous.yaml / server.rs::DEFAULT_CORE_PLUGIN）：
/// - `llm_call` → `pipeline_llm_core`
/// - `tool_execute` → `pipeline_tool_core`
///
/// 其他 core_type 按 `pipeline_{type}_core` 推断（无既有事实时兜底）。
fn core_plugin_id(core_type: &str) -> String {
    match core_type {
        "llm_call" => "pipeline_llm_core".to_string(),
        "tool_execute" => "pipeline_tool_core".to_string(),
        other => format!("{PLUGIN_ID_PREFIX}{other}_core"),
    }
}

/// 输出路由条目 → 引擎 Route（route_type 映射 + 条件保留）。
///
/// 0.1 与 0.2 路由语义差异处理：
/// - 0.1 `OutputRouteTable.arbitrate` 是「信号类型 + 条件」双匹配：wait 路由的
///   condition="True" 只在输出插件发出 wait 信号时才匹配（不会误触发）。
/// - 0.2 引擎 `apply_routes` 是纯条件匹配（无信号通道）：wait 条件 "True" 会先于
///   next_llm/end 匹配，导致纯文本回复时管道被误挂起。
/// - 修正：wait 路由的空/True 条件映射为对 wait 信号对应 state 字段的检测
///   `submitted_task_ids != [] and submitted_task_ids != None`——0.1 child_task_guard
///   发 wait 信号时写 `state_updates["submitted_task_ids"]`（active_ids），
///   对齐该确定性语义；显式非空/非 True 条件保留原样。
fn output_route_to_engine(entry: &OutputRouteEntry) -> Route {
    let (next, mut set) = match entry.route_type.as_str() {
        "next_tool" => (
            RouteNext::Loop,
            std::collections::HashMap::from([
                ("core_type".to_string(), serde_json::Value::String("tool_execute".to_string())),
                (
                    "core_plugin".to_string(),
                    serde_json::Value::String(core_plugin_id("tool_execute")),
                ),
            ]),
        ),
        "next_llm" => (
            RouteNext::Loop,
            std::collections::HashMap::from([
                ("core_type".to_string(), serde_json::Value::String("llm_call".to_string())),
                (
                    "core_plugin".to_string(),
                    serde_json::Value::String(core_plugin_id("llm_call")),
                ),
            ]),
        ),
        "wait" => (RouteNext::Wait, std::collections::HashMap::new()),
        _ => (RouteNext::End, std::collections::HashMap::new()),
    };
    // 保留 entry 的 target_core 覆盖（若有）
    if let Some(tc) = &entry.target_core {
        set.insert("core_type".to_string(), serde_json::Value::String(tc.clone()));
        set.insert(
            "core_plugin".to_string(),
            serde_json::Value::String(core_plugin_id(tc)),
        );
    }
    // 条件映射（0.1 信号驱动语义 → 0.2 纯条件匹配）：
    // - wait 路由：0.1 条件 "True" 只在 child_task_guard 发 wait 信号（写
    //   state_updates["submitted_task_ids"]）时命中 → 映射为对该字段的检测。
    // - next_llm 路由：0.1 条件 "True" 只在输出插件（如 task_reminder）显式发
    //   next_llm 信号时才继续 → 映射为 `next_llm_continue == true`（插件显式写
    //   该字段才命中；未写则落到 end 兜底结束，避免纯条件匹配下无限循环）。
    // - next_tool 路由：0.1 条件 `raw_tool_calls != []` 在 0.2 引擎中当字段缺失
    //   （Null）时 `Null != []` 求值为 true → 永远命中死循环。追加 None 保护，
    //   对齐 0.2 autonomous.yaml 的防御写法 `raw_tool_calls != [] and
    //   raw_tool_calls != None`。
    let when = if entry.route_type == "next_tool"
        && (entry.condition.is_empty()
            || entry.condition == "True"
            || entry.condition.trim() == "raw_tool_calls != []")
    {
        "raw_tool_calls != [] and raw_tool_calls != None".to_string()
    } else if entry.route_type == "wait"
        && (entry.condition.is_empty() || entry.condition == "True")
    {
        "submitted_task_ids != [] and submitted_task_ids != None".to_string()
    } else if entry.route_type == "next_llm"
        && (entry.condition.is_empty() || entry.condition == "True")
    {
        "next_llm_continue == true".to_string()
    } else if entry.condition.is_empty() {
        "True".to_string()
    } else {
        entry.condition.clone()
    };
    Route {
        when,
        then: RouteAction { next, set },
    }
}

impl PipelineDefinition {
    /// 转换为引擎 steps 模型（`agentos_core::types::PipelineConfig`）。
    ///
    /// 由 [`agentos_engine::PipelineExecutor`] 解释执行。转换规则见模块文档。
    pub fn to_engine_config(&self) -> PipelineConfig {
        PipelineConfig {
            name: self.name.clone(),
            // 0.1 默认循环：管道级 loop 开启，end 路由置 ended=true 终止
            loop_config: LoopConfig {
                enabled: true,
                ..Default::default()
            },
            steps: vec![
                self.make_prepare_step(),
                self.make_core_step(),
                self.make_post_step(),
            ],
            checkpoint: Default::default(),
        }
    }

    /// prepare 步骤：input 插件并集（保序去重，带 `pipeline_` 前缀）。
    fn make_prepare_step(&self) -> PipelineStep {
        let mut plugins: Vec<String> = Vec::new();
        let mut seen: HashSet<String> = HashSet::new();
        for route in &self.input_routes {
            if route.target != "core" {
                continue;
            }
            for p in &route.plugins {
                let id = to_plugin_id(p);
                if seen.insert(id.clone()) {
                    plugins.push(id);
                }
            }
        }
        PipelineStep {
            id: "prepare".to_string(),
            steps: plugins,
            context: std::collections::HashMap::new(),
            routes: vec![],
            loop_config: None,
        }
    }

    /// core 步骤：动态 core_plugin（llm_call 或 tool_execute，由 state.core_type 决定）。
    fn make_core_step(&self) -> PipelineStep {
        PipelineStep {
            id: "core".to_string(),
            steps: vec!["{{state.core_plugin}}".to_string()],
            context: std::collections::HashMap::new(),
            routes: vec![],
            loop_config: None,
        }
    }

    /// post 步骤：output 插件（未被 input_routes 引用的 plugins 条目）
    /// + 路由仲裁（output_routes 按 priority 排序转换）。
    fn make_post_step(&self) -> PipelineStep {
        let referenced: HashSet<&str> = self
            .input_routes
            .iter()
            .flat_map(|r| r.plugins.iter().map(|p| p.as_str()))
            .collect();
        let post_plugins: Vec<String> = self
            .plugins
            .iter()
            .filter(|p| !referenced.contains(p.name.as_str()))
            .map(|p| to_plugin_id(&p.name))
            .collect();

        let mut routes_sorted = self.output_routes.clone();
        routes_sorted.sort_by_key(|r| r.priority);
        let routes: Vec<Route> = routes_sorted.iter().map(output_route_to_engine).collect();

        PipelineStep {
            id: "post".to_string(),
            steps: post_plugins,
            context: std::collections::HashMap::new(),
            routes,
            loop_config: None,
        }
    }
}

// ── Agent 配置 ───────────────────────────────────────────────────

/// Agent 配置（`config/agents/**/{id}.yaml` 关键字段）。
///
/// 与 0.1 `config/agents/*.yaml` 字段对应：config_id / level / model_tier /
/// system_prompt / tool_ids / max_iterations。缺失字段用默认值。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct AgentConfig {
    pub config_id: String,
    #[serde(default)]
    pub name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub level: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model_tier: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub system_prompt: Option<String>,
    #[serde(default)]
    pub tool_ids: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_iterations: Option<i32>,
}

/// 加载 Agent 配置（`config/agents/**/{id}.yaml`，递归搜索分类子目录）。
///
/// 文件不存在 → 返回默认空配置（config_id 填充为入参，不报错——调用方用默认值）。
///
/// # Errors
/// - [`ConfigError::YamlParse`]：文件存在但解析失败（暴露坏配置）。
pub fn load_agent_config(config_root: &Path, agent_id: &str) -> Result<AgentConfig, ConfigError> {
    let agents_dir = config_root.join("agents");
    let found = find_agent_yaml(&agents_dir, agent_id);
    let Some(path) = found else {
        return Ok(AgentConfig {
            config_id: agent_id.to_string(),
            ..Default::default()
        });
    };

    let content = std::fs::read_to_string(&path).map_err(|e| ConfigError::Io {
        message: format!("read {} failed: {e}", path.display()),
    })?;
    let mut cfg: AgentConfig = serde_yaml::from_str(&content).map_err(|e| ConfigError::YamlParse {
        path: path.to_string_lossy().to_string(),
        message: e.to_string(),
    })?;
    // config_id 缺失时用文件名兜底
    if cfg.config_id.is_empty() {
        cfg.config_id = agent_id.to_string();
    }
    Ok(cfg)
}

/// 在 agents 目录（含分类子目录）递归查找 `<agent_id>.yaml`。
pub(crate) fn find_agent_yaml(dir: &Path, agent_id: &str) -> Option<std::path::PathBuf> {
    let target = format!("{agent_id}.yaml");
    let entries = std::fs::read_dir(dir).ok()?;
    for entry in entries.flatten() {
        let p = entry.path();
        if p.is_dir() {
            if let Some(found) = find_agent_yaml(&p, agent_id) {
                return Some(found);
            }
        } else if p.file_name().map(|n| n == target.as_str()).unwrap_or(false) {
            return Some(p);
        }
    }
    None
}
