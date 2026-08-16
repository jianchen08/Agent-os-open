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
    let mut cfg: AgentConfig =
        serde_yaml::from_str(&content).map_err(|e| ConfigError::YamlParse {
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
    // 两轮：先按文件名精确匹配（快路径），未命中再按 yaml 内 config_id 匹配——
    // 执行 agent 的文件名常与 config_id 不同（code_writer.yaml 的
    // config_id=code_writer_agent，任务派发引用的是 config_id）。
    let mut fallback: Vec<std::path::PathBuf> = Vec::new();
    for entry in entries.flatten() {
        let p = entry.path();
        if p.is_dir() {
            if let Some(found) = find_agent_yaml(&p, agent_id) {
                return Some(found);
            }
        } else if p.file_name().map(|n| n == target.as_str()).unwrap_or(false) {
            return Some(p);
        } else if p.extension().map(|e| e == "yaml").unwrap_or(false) {
            fallback.push(p);
        }
    }
    for p in fallback {
        if let Ok(raw) = std::fs::read_to_string(&p) {
            if let Ok(cfg) = serde_yaml::from_str::<serde_yaml::Value>(&raw) {
                if cfg.get("config_id").and_then(|v| v.as_str()) == Some(agent_id) {
                    return Some(p);
                }
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write_agent_yaml(root: &std::path::Path, rel: &str, content: &str) {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, content).unwrap();
    }

    #[test]
    fn load_agent_config_missing_file_returns_default_with_config_id() {
        let root = tempfile::tempdir().unwrap();
        // agents/ 目录都不存在
        let cfg = load_agent_config(root.path(), "ghost_agent").unwrap();
        assert_eq!(cfg.config_id, "ghost_agent");
        assert!(cfg.name.is_empty());
        assert!(cfg.tool_ids.is_empty());
        assert!(cfg.system_prompt.is_none());
        assert!(cfg.max_iterations.is_none());
    }

    #[test]
    fn load_agent_config_parses_full_fields() {
        let root = tempfile::tempdir().unwrap();
        write_agent_yaml(
            root.path(),
            "agents/researcher.yaml",
            "config_id: researcher-v1\n\
             name: Researcher\n\
             level: senior\n\
             model_tier: fast\n\
             system_prompt: You are a researcher\n\
             tool_ids: [web_fetch, memory_read]\n\
             max_iterations: 8\n",
        );
        let cfg = load_agent_config(root.path(), "researcher").unwrap();
        assert_eq!(cfg.config_id, "researcher-v1");
        assert_eq!(cfg.name, "Researcher");
        assert_eq!(cfg.level.as_deref(), Some("senior"));
        assert_eq!(cfg.model_tier.as_deref(), Some("fast"));
        assert_eq!(cfg.system_prompt.as_deref(), Some("You are a researcher"));
        assert_eq!(cfg.tool_ids, vec!["web_fetch", "memory_read"]);
        assert_eq!(cfg.max_iterations, Some(8));
    }

    #[test]
    fn load_agent_config_missing_config_id_falls_back_to_filename() {
        let root = tempfile::tempdir().unwrap();
        // config_id 显式为空串（serde 必填字段，缺字段会解析失败）→ 文件名兜底。
        write_agent_yaml(
            root.path(),
            "agents/plain.yaml",
            "config_id: ''\nname: Plain\n",
        );
        let cfg = load_agent_config(root.path(), "plain").unwrap();
        assert_eq!(cfg.config_id, "plain", "config_id 为空时用文件名兜底");
        assert_eq!(cfg.name, "Plain");
    }

    #[test]
    fn load_agent_config_finds_nested_category_dir() {
        let root = tempfile::tempdir().unwrap();
        // agents/main/deep_think.yaml（分类子目录）
        write_agent_yaml(
            root.path(),
            "agents/main/deep_think.yaml",
            "config_id: deep_think\nname: Deep Think\n",
        );
        let cfg = load_agent_config(root.path(), "deep_think").unwrap();
        assert_eq!(cfg.config_id, "deep_think");
        assert_eq!(cfg.name, "Deep Think");
    }

    #[test]
    fn load_agent_config_yaml_parse_error_is_exposed() {
        let root = tempfile::tempdir().unwrap();
        write_agent_yaml(root.path(), "agents/broken.yaml", "config_id: [unclosed\n");
        let err = load_agent_config(root.path(), "broken").unwrap_err();
        match err {
            ConfigError::YamlParse { path, .. } => {
                assert!(path.contains("broken.yaml"), "got: {path}")
            }
            other => panic!("expected YamlParse, got {other:?}"),
        }
    }

    #[test]
    fn load_agent_config_unknown_fields_ignored() {
        // serde 默认忽略未知字段——新加的 agent 配置字段不破坏旧内核。
        let root = tempfile::tempdir().unwrap();
        write_agent_yaml(
            root.path(),
            "agents/future.yaml",
            "config_id: future\nname: Future\nsome_future_field: 42\n",
        );
        let cfg = load_agent_config(root.path(), "future").unwrap();
        assert_eq!(cfg.config_id, "future");
        assert_eq!(cfg.name, "Future");
    }

    #[test]
    fn find_agent_yaml_returns_none_for_missing_dir_or_agent() {
        let root = tempfile::tempdir().unwrap();
        assert!(find_agent_yaml(&root.path().join("agents"), "x").is_none());
        write_agent_yaml(root.path(), "agents/a.yaml", "name: A\n");
        assert!(find_agent_yaml(&root.path().join("agents"), "missing").is_none());
    }

    #[test]
    fn find_agent_yaml_does_not_match_other_extensions() {
        let root = tempfile::tempdir().unwrap();
        write_agent_yaml(root.path(), "agents/x.yaml.bak", "name: X\n");
        write_agent_yaml(root.path(), "agents/y.txt", "name: Y\n");
        assert!(find_agent_yaml(&root.path().join("agents"), "x").is_none());
        assert!(find_agent_yaml(&root.path().join("agents"), "y").is_none());
    }
}
