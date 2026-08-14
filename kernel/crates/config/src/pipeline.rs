//! # Agent 配置承载（P7）
//!
//! 内核 config crate 承载 Agent 配置——管道配置（`config/pipelines/*.yaml`）
//! 由 api crate 的 `pipeline_loader` 直接解析为引擎模型
//! [`agentos_core::types::PipelineConfig`]（多循环体：init → main → exit），
//! 本模块只负责 Agent 侧配置。
//!
//! ## 模块组织
//!
//! - [`AgentConfig`] / [`load_agent_config`]：加载 `config/agents/**/{id}.yaml`
//!   的关键字段（config_id / level / model_tier / system_prompt / tool_ids /
//!   max_iterations），供引擎注入管道 state。
//! - [`find_agent_yaml`]：在 agents 目录（含分类子目录）递归定位
//!   `<agent_id>.yaml`。
//!
//! [来源: docs/working/frontend_01_alignment_plan.md §P7]

use std::path::Path;

use serde::{Deserialize, Serialize};

use crate::error::ConfigError;

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
