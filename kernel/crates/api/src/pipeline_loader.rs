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

use std::collections::HashSet;
use std::path::{Path, PathBuf};

use agentos_core::types::{PipelineConfig, PipelineStep, StepLibrary};

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
            "Pipeline config not found at {}, using default (loop disabled, empty steps)",
            path.display()
        );
        return Ok(PipelineConfig {
            name: "default".to_string(),
            loop_config: Default::default(),
            steps: Vec::new(),
        });
    }
    let raw = std::fs::read_to_string(&path)
        .map_err(|e| PipelineLoadError::ReadFile(path.clone(), e.to_string()))?;
    let config: PipelineConfig = serde_yaml::from_str(&raw)
        .map_err(|e| PipelineLoadError::ParseYaml(path.clone(), e.to_string()))?;
    Ok(config)
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
        let step: PipelineStep = match serde_yaml::from_str(&raw) {
            Ok(s) => s,
            Err(e) => {
                // 解析失败：归并到 Err 列表（致命，让启动期暴露坏配置）
                return Err(PipelineLoadError::ParseYaml(path, e.to_string()));
            }
        };
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
/// 三类冲突（任一命中返回 `Err`，信息含具体冲突 id 与来源）：
/// ① pipeline.steps 的 id 之间重复：`"step id 'X' 重复（在 pipeline 配置中）"`
/// ② pipeline.steps 的 id 在 `plugin_ids` 中：`"step id 'X' 与插件 id 冲突"`
/// ③ step_library 的 id 在 `plugin_ids` 中：`"step id 'X' 与插件 id 冲突"`
///
/// 设计取舍：所有冲突一次性收集后返回第一条（首个报错即退出，避免错误信息噪声）。
pub fn validate_no_name_conflicts(
    pipeline: &PipelineConfig,
    step_library: &StepLibrary,
    plugin_ids: &HashSet<String>,
) -> Result<(), String> {
    // ① pipeline.steps id 之间不能重复
    let mut seen: HashSet<&str> = HashSet::new();
    for step in &pipeline.steps {
        if !seen.insert(step.id.as_str()) {
            return Err(format!(
                "step id '{}' 重复（在 pipeline 配置中）",
                step.id
            ));
        }
    }

    // ② pipeline.steps id 不能与插件 id 冲突
    for step in &pipeline.steps {
        if plugin_ids.contains(&step.id) {
            return Err(format!(
                "step id '{}' 与插件 id 冲突（pipeline 配置）",
                step.id
            ));
        }
    }

    // ③ step_library id 不能与插件 id 冲突
    for id in step_library.steps.keys() {
        if plugin_ids.contains(id) {
            return Err(format!(
                "step id '{}' 与插件 id 冲突（公共 step 库）",
                id
            ));
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
loop:
  enabled: true
  max_iterations: 5
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
        assert!(cfg.loop_config.enabled);
        assert_eq!(cfg.loop_config.max_iterations, 5);
        assert_eq!(cfg.steps.len(), 1);
        assert_eq!(cfg.steps[0].id, "prepare");
        assert_eq!(cfg.steps[0].steps, vec!["tool_schema".to_string()]);
        assert_eq!(cfg.steps[0].context.get("agent_id").unwrap(), "A1");
    }

    /// 文件不存在 → 返回默认配置（不报错）。
    #[test]
    fn test_load_pipeline_config_missing_returns_default() {
        let tmp = TempDir::new().unwrap();
        let cfg = load_pipeline_config(tmp.path()).expect("missing config should not error");
        assert!(!cfg.loop_config.enabled);
        assert!(cfg.steps.is_empty());
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

    /// validate_no_name_conflicts：无冲突 → Ok。
    #[test]
    fn test_validate_no_conflicts_ok() {
        let pipeline = PipelineConfig {
            name: "p".into(),
            loop_config: Default::default(),
            steps: vec![PipelineStep {
                id: "s1".into(),
                steps: vec![],
                context: HashMap::new(),
                routes: vec![],
                loop_config: None,
            }],
        };
        let mut lib = StepLibrary::default();
        lib.steps.insert(
            "lib_a".into(),
            PipelineStep {
                id: "lib_a".into(),
                steps: vec![],
                context: HashMap::new(),
                routes: vec![],
                loop_config: None,
            },
        );
        let mut plugin_ids = HashSet::new();
        plugin_ids.insert("plugin_x".to_string());
        assert!(validate_no_name_conflicts(&pipeline, &lib, &plugin_ids).is_ok());
    }

    /// 冲突①：pipeline.steps id 重复。
    #[test]
    fn test_validate_conflict_duplicate_pipeline_step_id() {
        let pipeline = PipelineConfig {
            name: "p".into(),
            loop_config: Default::default(),
            steps: vec![
                PipelineStep {
                    id: "dup".into(),
                    steps: vec![],
                    context: HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                },
                PipelineStep {
                    id: "dup".into(),
                    steps: vec![],
                    context: HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                },
            ],
        };
        let lib = StepLibrary::default();
        let plugin_ids = HashSet::new();
        let err = validate_no_name_conflicts(&pipeline, &lib, &plugin_ids).unwrap_err();
        assert!(err.contains("dup"), "err should name the conflicting id: {err}");
        assert!(err.contains("重复"));
    }

    /// 冲突②：pipeline.steps id 与插件 id 冲突。
    #[test]
    fn test_validate_conflict_pipeline_step_vs_plugin_id() {
        let pipeline = PipelineConfig {
            name: "p".into(),
            loop_config: Default::default(),
            steps: vec![PipelineStep {
                id: "shared".into(),
                steps: vec![],
                context: HashMap::new(),
                routes: vec![],
                loop_config: None,
            }],
        };
        let lib = StepLibrary::default();
        let mut plugin_ids = HashSet::new();
        plugin_ids.insert("shared".to_string());
        let err = validate_no_name_conflicts(&pipeline, &lib, &plugin_ids).unwrap_err();
        assert!(err.contains("shared"));
        assert!(err.contains("插件"));
    }

    /// 冲突③：step_library id 与插件 id 冲突。
    #[test]
    fn test_validate_conflict_library_step_vs_plugin_id() {
        let pipeline = PipelineConfig {
            name: "p".into(),
            loop_config: Default::default(),
            steps: vec![],
        };
        let mut lib = StepLibrary::default();
        lib.steps.insert(
            "doc_extract".into(),
            PipelineStep {
                id: "doc_extract".into(),
                steps: vec![],
                context: HashMap::new(),
                routes: vec![],
                loop_config: None,
            },
        );
        let mut plugin_ids = HashSet::new();
        plugin_ids.insert("doc_extract".to_string());
        let err = validate_no_name_conflicts(&pipeline, &lib, &plugin_ids).unwrap_err();
        assert!(err.contains("doc_extract"));
        assert!(err.contains("插件"));
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
loop:
  enabled: true
  max_iterations: -1
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
routes:
  - when: "extract_result == ''"
    then:
      next: end
      set:
        status: failed
"#,
        )
        .unwrap();

        let cfg = load_pipeline_config(root).expect("pipeline config");
        assert_eq!(cfg.name, "autonomous");
        assert_eq!(cfg.steps.len(), 1);
        assert_eq!(cfg.steps[0].id, "prepare");

        let lib = load_step_library(root).expect("step library");
        assert!(lib.steps.contains_key("doc_extract"));

        let plugin_ids = HashSet::new();
        assert!(validate_no_name_conflicts(&cfg, &lib, &plugin_ids).is_ok());
    }
}
