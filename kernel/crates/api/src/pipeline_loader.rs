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

use agentos_core::types::{PipelineConfig, PipelineStep, RouteNext, StepLibrary};

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
            return Err(format!("循环体 id '{}' 重复（在 pipeline 配置中）", body.id));
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
    let body_ids: HashSet<&str> = pipeline
        .loop_bodies
        .iter()
        .map(|b| b.id.as_str())
        .collect();
    for step in pipeline
        .loop_bodies
        .iter()
        .flat_map(|b| b.steps.iter())
    {
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
    loop_config:
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
        assert_eq!(cfg.loop_bodies.len(), 2);
        assert_eq!(cfg.loop_bodies[0].id, "init");
        assert!(!cfg.loop_bodies[0].loop_config.as_ref().map(|c| c.enabled).unwrap_or(false));
        let main = &cfg.loop_bodies[1];
        assert_eq!(main.id, "main");
        assert!(main.loop_config.as_ref().unwrap().enabled);
        assert_eq!(main.loop_config.as_ref().unwrap().max_iterations, 5);
        assert_eq!(main.steps.len(), 1);
        assert_eq!(main.steps[0].id, "prepare");
        assert_eq!(main.steps[0].steps, vec!["tool_schema".to_string()]);
        assert_eq!(main.steps[0].context.get("agent_id").unwrap(), "A1");
    }

    /// 文件不存在 → 返回默认配置（不报错）。
    #[test]
    fn test_load_pipeline_config_missing_returns_default() {
        let tmp = TempDir::new().unwrap();
        let cfg = load_pipeline_config(tmp.path()).expect("missing config should not error");
        assert!(cfg.loop_bodies.is_empty());
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
                loop_config: None,
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
                    loop_config: None,
                    exit_routes: vec![],
                    run_on_error: false,
                },
                LoopBody {
                    id: "main".into(),
                    steps: vec![],
                    loop_config: None,
                    exit_routes: vec![],
                    run_on_error: false,
                },
            ],
            checkpoint: Default::default(),
        };
        let lib = StepLibrary::default();
        let plugin_ids = HashSet::new();
        let err = validate_no_name_conflicts(&pipeline, &lib, &plugin_ids).unwrap_err();
        assert!(err.contains("main"), "err should name the conflicting id: {err}");
        assert!(err.contains("循环体"));
    }

    /// 冲突②：pipeline step id 重复。
    #[test]
    fn test_validate_conflict_duplicate_pipeline_step_id() {
        let pipeline = single_body_pipeline("p", vec![make_step("dup"), make_step("dup")]);
        let lib = StepLibrary::default();
        let plugin_ids = HashSet::new();
        let err = validate_no_name_conflicts(&pipeline, &lib, &plugin_ids).unwrap_err();
        assert!(err.contains("dup"), "err should name the conflicting id: {err}");
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
        lib.steps.insert("doc_extract".into(), make_step("doc_extract"));
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
                loop_config: None,
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
    loop_config:
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
        assert_eq!(cfg.loop_bodies.len(), 1);
        assert_eq!(cfg.loop_bodies[0].id, "main");
        assert_eq!(cfg.loop_bodies[0].steps.len(), 1);
        assert_eq!(cfg.loop_bodies[0].steps[0].id, "prepare");

        let lib = load_step_library(root).expect("step library");
        assert!(lib.steps.contains_key("doc_extract"));

        let plugin_ids = HashSet::new();
        assert!(validate_no_name_conflicts(&cfg, &lib, &plugin_ids).is_ok());
    }
}
