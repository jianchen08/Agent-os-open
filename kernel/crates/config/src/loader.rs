//! 配置加载器
//!
//! 对应 0.1 的 `src/config/loader.py ConfigLoader`。
//! 负责 YAML 文件加载、环境变量插值（`${VAR}` / `${VAR:-default}`）、
//! 外部文件引用解析（`{{path:filename}}`）。
//!
//! [来源: src/config/loader.py]
//!
//! DEBT: ADR 文档要求使用 serde_yml（社区维护 fork），但 crates.io 上
//! serde_yml 与 serde_yaml API 不同。当前使用 serde_yaml 0.9（dtolnay 维护），
//! 功能完整且 anchor/alias/merge key 测试通过。ceiling: serde_yaml 可能不再更新。
//! upgrade: 若 serde_yml API 稳定后切换。

use std::collections::HashMap;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};

use regex::Regex;
use serde_json::Value;

use crate::error::ConfigError;

/// 环境变量插值正则：`${VAR}` 或 `${VAR:-default}`
///
/// 与 0.1 Python 正则 `\$\\{([^}:]+)(?::-([^}]*))?\\}` 完全一致。
/// [来源: src/config/loader.py L32]
const ENV_VAR_PATTERN: &str = r"\$\{([^}:]+)(?::-([^}]*))?\}";

/// 外部文件引用正则：`{{path:filename}}` 或 `{{path:dir|extensions=.md,.yaml}}`
///
/// 与 0.1 PromptBuildPlugin._resolve_placeholders 中的 path 类型一致。
/// [来源: src/plugins/input/prompt_build/plugin.py L115-117]
const PATH_REF_PATTERN: &str = r"\{\{path:([^}]+)\}\}";

/// 配置加载器。
///
/// 负责：
/// 1. 加载 YAML 文件并解析为 `serde_json::Value`
/// 2. 递归替换环境变量 `${VAR}` / `${VAR:-default}`
/// 3. 解析外部文件引用 `{{path:filename}}`
/// 4. 批量加载目录下所有 YAML 文件
///
/// 环境变量优先级：系统环境变量 > .env 文件 > 默认值
pub struct ConfigLoader {
    config_dir: PathBuf,
    env_vars: HashMap<String, String>,
    env_re: Regex,
    path_re: Regex,
}

impl ConfigLoader {
    /// 创建配置加载器。
    pub fn new(config_dir: impl Into<PathBuf>, env_file: Option<PathBuf>) -> Self {
        let mut loader = Self {
            config_dir: config_dir.into(),
            env_vars: HashMap::new(),
            env_re: Regex::new(ENV_VAR_PATTERN).expect("invalid env var regex"),
            path_re: Regex::new(PATH_REF_PATTERN).expect("invalid path ref regex"),
        };
        if let Some(env_path) = env_file {
            loader.load_env_file(&env_path);
        }
        loader
    }

    /// 加载 .env 文件到内部环境变量字典。
    fn load_env_file(&mut self, env_file: &Path) {
        if !env_file.exists() {
            return;
        }
        if let Ok(content) = fs::read_to_string(env_file) {
            for line in content.lines() {
                let line = line.trim();
                if line.is_empty() || line.starts_with('#') {
                    continue;
                }
                if let Some((key, value)) = line.split_once('=') {
                    self.env_vars
                        .insert(key.trim().to_string(), value.trim().to_string());
                }
            }
        }
    }

    /// 获取环境变量值（三级优先级）。
    ///
    /// 优先级：系统环境变量 > .env 文件 > 默认值
    fn get_env_var(&self, var_name: &str, default: Option<&str>) -> Result<String, ConfigError> {
        if let Ok(value) = env::var(var_name) {
            return Ok(value);
        }
        if let Some(value) = self.env_vars.get(var_name) {
            return Ok(value.clone());
        }
        if let Some(def) = default {
            return Ok(def.to_string());
        }
        Err(ConfigError::EnvVarNotFound {
            var_name: var_name.to_string(),
        })
    }

    /// 递归替换 JSON Value 中的环境变量。
    ///
    /// 支持 `${VAR}`（必需）和 `${VAR:-default}`（带默认值）。
    ///
    /// 与 0.1 Python 行为一致：环境变量未设置且无默认值时返回 `ConfigError::EnvVarNotFound`。
    pub fn substitute_env_vars(&self, value: &Value) -> Result<Value, ConfigError> {
        match value {
            Value::String(s) => {
                let mut error: Option<ConfigError> = None;
                let replaced = self.env_re.replace_all(s, |caps: &regex::Captures| {
                    let var_name = caps.get(1).map(|m| m.as_str()).unwrap_or("");
                    let default = caps.get(2).map(|m| m.as_str());
                    match self.get_env_var(var_name, default) {
                        Ok(val) => val,
                        Err(e) => {
                            error = Some(e);
                            String::new()
                        }
                    }
                });
                if let Some(e) = error {
                    return Err(e);
                }
                Ok(Value::String(replaced.to_string()))
            }
            Value::Object(map) => {
                let mut new_map = serde_json::Map::new();
                for (k, v) in map {
                    new_map.insert(k.clone(), self.substitute_env_vars(v)?);
                }
                Ok(Value::Object(new_map))
            }
            Value::Array(arr) => {
                let mut new_arr = Vec::with_capacity(arr.len());
                for v in arr {
                    new_arr.push(self.substitute_env_vars(v)?);
                }
                Ok(Value::Array(new_arr))
            }
            other => Ok(other.clone()),
        }
    }

    /// 解析外部文件引用 `{{path:filename}}`。
    ///
    /// 读取指定路径的文件内容，替换引用。
    /// 路径相对于 `config_dir` 的父目录（项目根目录）。
    pub fn resolve_path_refs(&self, content: &str) -> Result<String, ConfigError> {
        let project_root = self
            .config_dir
            .parent()
            .unwrap_or(Path::new("."))
            .to_path_buf();

        let mut error: Option<ConfigError> = None;

        let result = self.path_re.replace_all(content, |caps: &regex::Captures| {
            let ref_path = caps.get(1).map(|m| m.as_str()).unwrap_or("");

            let (raw_path, extensions) = if let Some((p, exts)) = ref_path.split_once('|') {
                let exts_str = exts.strip_prefix("extensions=").unwrap_or("");
                (p.trim(), Some(exts_str))
            } else {
                (ref_path.trim(), None)
            };

            let resolved = project_root.join(raw_path);

            if resolved.is_dir() {
                let mut combined = String::new();
                if let Ok(entries) = fs::read_dir(&resolved) {
                    let mut files: Vec<_> = entries
                        .flatten()
                        .filter(|e| {
                            e.path().is_file() && should_include_file(&e.path(), extensions)
                        })
                        .collect();
                    files.sort_by_key(|e| e.path());
                    for file in files {
                        match fs::read_to_string(file.path()) {
                            Ok(content) => {
                                combined.push_str(&content);
                                combined.push('\n');
                            }
                            Err(e) => {
                                error = Some(ConfigError::Io {
                                    message: format!(
                                        "Failed to read {}: {}",
                                        file.path().display(),
                                        e
                                    ),
                                });
                            }
                        }
                    }
                }
                combined
            } else if resolved.is_file() {
                match fs::read_to_string(&resolved) {
                    Ok(content) => content,
                    Err(e) => {
                        error = Some(ConfigError::Io {
                            message: format!("Failed to read {}: {}", resolved.display(), e),
                        });
                        String::new()
                    }
                }
            } else {
                error = Some(ConfigError::PathRefFailed {
                    ref_path: raw_path.to_string(),
                    source_path: project_root.to_string_lossy().to_string(),
                });
                String::new()
            }
        });

        if let Some(e) = error {
            return Err(e);
        }

        Ok(result.to_string())
    }

    /// 加载单个 YAML 配置文件。
    pub fn load_yaml(&self, relative_path: &str) -> Result<Value, ConfigError> {
        let file_path = self.config_dir.join(relative_path);
        if !file_path.exists() {
            return Err(ConfigError::NotFound {
                path: file_path.to_string_lossy().to_string(),
            });
        }

        let content = fs::read_to_string(&file_path).map_err(|e| ConfigError::Io {
            message: e.to_string(),
        })?;

        self.parse_yaml(&content, &file_path.to_string_lossy())
    }

    /// 解析 YAML 字符串为 Value（带环境变量插值）。
    pub fn parse_yaml(&self, content: &str, source_path: &str) -> Result<Value, ConfigError> {
        let resolved = self.resolve_path_refs(content)?;

        let yaml_value: serde_yaml::Value =
            serde_yaml::from_str(&resolved).map_err(|e| ConfigError::YamlParse {
                path: source_path.to_string(),
                message: e.to_string(),
            })?;

        let expanded = expand_merge_keys(yaml_value);

        let parsed: Value =
            serde_yaml::from_value(expanded).map_err(|e| ConfigError::YamlParse {
                path: source_path.to_string(),
                message: format!("YAML to JSON conversion error: {}", e),
            })?;

        self.substitute_env_vars(&parsed)
    }

    /// 批量加载 config_dir 下所有 .yaml/.yml 文件。
    ///
    /// 返回 HashMap<文件名(不含扩展名), Value>。
    /// 遇到 YAML 解析错误时传播错误（不吞异常）。
    pub fn load_all(&self) -> Result<HashMap<String, Value>, ConfigError> {
        let mut result = HashMap::new();

        if !self.config_dir.exists() {
            return Ok(result);
        }

        let entries = fs::read_dir(&self.config_dir).map_err(|e| ConfigError::Io {
            message: e.to_string(),
        })?;

        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().is_some_and(|e| e == "yaml" || e == "yml") {
                let content = fs::read_to_string(&path).map_err(|e| ConfigError::Io {
                    message: format!("Failed to read {}: {}", path.display(), e),
                })?;

                let stem = path
                    .file_stem()
                    .map(|s| s.to_string_lossy().to_string())
                    .unwrap_or_default();

                let value = self.parse_yaml(&content, &path.to_string_lossy())?;
                result.insert(stem, value);
            }
        }

        Ok(result)
    }

    /// 获取配置目录引用。
    pub fn config_dir(&self) -> &Path {
        &self.config_dir
    }

    /// 获取 .env 变量字典引用。
    pub fn env_vars(&self) -> &HashMap<String, String> {
        &self.env_vars
    }
}

/// 判断文件是否应该被包含（基于扩展名过滤）。
fn should_include_file(path: &Path, extensions: Option<&str>) -> bool {
    match extensions {
        Some(exts) => {
            let ext = path.extension().map(|e| e.to_string_lossy().to_string());
            match ext {
                Some(file_ext) => exts.split(',').any(|e| {
                    e.trim()
                        .trim_start_matches('.')
                        .eq_ignore_ascii_case(&file_ext)
                }),
                None => false,
            }
        }
        None => true,
    }
}

/// 递归展开 YAML merge key（`<<` 语法）。
fn expand_merge_keys(value: serde_yaml::Value) -> serde_yaml::Value {
    match value {
        serde_yaml::Value::Mapping(mut map) => {
            for (_, v) in map.iter_mut() {
                *v = expand_merge_keys(v.clone());
            }

            let merge_key = serde_yaml::Value::String("<<".to_string());
            if let Some(merge_val) = map.remove(&merge_key) {
                match merge_val {
                    serde_yaml::Value::Mapping(merge_map) => {
                        for (k, v) in merge_map {
                            if !map.contains_key(&k) {
                                map.insert(k, v);
                            }
                        }
                    }
                    serde_yaml::Value::Sequence(seq) => {
                        for item in seq {
                            if let serde_yaml::Value::Mapping(merge_map) = item {
                                for (k, v) in merge_map {
                                    if !map.contains_key(&k) {
                                        map.insert(k, v);
                                    }
                                }
                            }
                        }
                    }
                    _ => {}
                }
            }

            serde_yaml::Value::Mapping(map)
        }
        serde_yaml::Value::Sequence(seq) => {
            serde_yaml::Value::Sequence(seq.into_iter().map(expand_merge_keys).collect())
        }
        other => other,
    }
}

/// 组合插件步骤配置（ADR ⑥）。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct StepConfig {
    pub name: String,
    pub plugin: String,
    #[serde(default)]
    pub inputs: Value,
    #[serde(default)]
    pub outputs: HashMap<String, String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub condition: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub then_steps: Vec<StepConfig>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub else_steps: Vec<StepConfig>,
}

/// 组合插件配置（ADR ⑥⑪）。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct CompositePluginYaml {
    pub id: String,
    pub name: String,
    pub version: String,
    pub plugin_type: String,
    pub steps: Vec<StepConfig>,
}

impl CompositePluginYaml {
    /// 从 YAML 字符串解析组合插件配置。
    pub fn from_yaml_str(yaml: &str) -> Result<Self, ConfigError> {
        let config: Self = serde_yaml::from_str(yaml).map_err(|e| ConfigError::Composite {
            message: format!("YAML parse error: {}", e),
        })?;

        if config.plugin_type != "composite" {
            return Err(ConfigError::Composite {
                message: format!(
                    "expected plugin_type='composite', got '{}'",
                    config.plugin_type
                ),
            });
        }

        if config.steps.is_empty() {
            return Err(ConfigError::Composite {
                message: "composite plugin must have at least one step".to_string(),
            });
        }

        Ok(config)
    }

    /// 验证步骤中的变量插值语法。
    pub fn validate_step_vars(&self) -> Result<(), ConfigError> {
        let var_re = Regex::new(r"\{\{state\.([^}]+)\}\}").expect("invalid var regex");

        for step in &self.steps {
            Self::validate_single_step(step, &var_re)?;
        }
        Ok(())
    }

    fn validate_single_step(step: &StepConfig, var_re: &Regex) -> Result<(), ConfigError> {
        let inputs_str = serde_json::to_string(&step.inputs).unwrap_or_default();
        for cap in var_re.captures_iter(&inputs_str) {
            let var_name = cap.get(1).map(|m| m.as_str()).unwrap_or("");
            if var_name.is_empty() {
                return Err(ConfigError::Composite {
                    message: format!("step '{}' has empty state variable reference", step.name),
                });
            }
        }
        Self::validate_sub_steps(&step.then_steps, &step.name, var_re)?;
        Self::validate_sub_steps(&step.else_steps, &step.name, var_re)?;
        Ok(())
    }

    fn validate_sub_steps(
        steps: &[StepConfig],
        parent: &str,
        var_re: &Regex,
    ) -> Result<(), ConfigError> {
        for step in steps {
            Self::validate_single_step(step, var_re).map_err(|e| ConfigError::Composite {
                message: format!("in step '{}/{}': {}", parent, step.name, e),
            })?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_env_var_substitution_simple() {
        unsafe {
            env::set_var("TEST_VAR_42", "hello");
        }
        let loader = ConfigLoader::new("/tmp", None);
        let input = Value::String("${TEST_VAR_42}".to_string());
        let result = loader.substitute_env_vars(&input).unwrap();
        assert_eq!(result, Value::String("hello".to_string()));
        unsafe {
            env::remove_var("TEST_VAR_42");
        }
    }

    #[test]
    fn test_env_var_substitution_with_default() {
        let loader = ConfigLoader::new("/tmp", None);
        let input = Value::String("${NONEXISTENT_VAR_999:-fallback}".to_string());
        let result = loader.substitute_env_vars(&input).unwrap();
        assert_eq!(result, Value::String("fallback".to_string()));
    }

    #[test]
    fn test_env_var_substitution_nested() {
        unsafe {
            env::set_var("NESTED_VAR", "world");
        }
        let loader = ConfigLoader::new("/tmp", None);
        let input = serde_json::json!({
            "key": "${NESTED_VAR}",
            "list": ["${NESTED_VAR}", "plain"],
            "nested": { "inner": "${NESTED_VAR:-default}" }
        });
        let result = loader.substitute_env_vars(&input).unwrap();
        assert_eq!(result["key"], "world");
        assert_eq!(result["list"][0], "world");
        assert_eq!(result["list"][1], "plain");
        assert_eq!(result["nested"]["inner"], "world");
        unsafe {
            env::remove_var("NESTED_VAR");
        }
    }

    #[test]
    fn test_env_var_missing_no_default_returns_error() {
        // AC-03-2: 与 0.1 Python 行为一致——未设置且无默认值时返回错误
        let loader = ConfigLoader::new("/tmp", None);
        let input = Value::String("${DEFINITELY_NOT_SET_VAR_42}".to_string());
        let result = loader.substitute_env_vars(&input);
        assert!(result.is_err());
        match result {
            Err(ConfigError::EnvVarNotFound { var_name }) => {
                assert_eq!(var_name, "DEFINITELY_NOT_SET_VAR_42");
            }
            _ => panic!("Expected EnvVarNotFound error"),
        }
    }

    #[test]
    fn test_env_file_loading() {
        let temp = tempfile::NamedTempFile::new().unwrap();
        fs::write(temp.path(), "MY_TEST_KEY=my_test_value\n# comment\n\n").unwrap();
        let loader = ConfigLoader::new("/tmp", Some(temp.path().to_path_buf()));
        assert_eq!(
            loader.env_vars().get("MY_TEST_KEY"),
            Some(&"my_test_value".to_string())
        );
    }

    #[test]
    fn test_env_priority_system_over_env_file() {
        let temp = tempfile::NamedTempFile::new().unwrap();
        fs::write(temp.path(), "PRIORITY_TEST=from_file\n").unwrap();
        unsafe {
            env::set_var("PRIORITY_TEST", "from_system");
        }
        let loader = ConfigLoader::new("/tmp", Some(temp.path().to_path_buf()));
        let result = loader.get_env_var("PRIORITY_TEST", None);
        assert_eq!(result.unwrap(), "from_system");
        unsafe {
            env::remove_var("PRIORITY_TEST");
        }
    }

    #[test]
    fn test_composite_plugin_parse() {
        let yaml = r#"
id: rag_generator
name: RAG Generator
version: "1.0.0"
plugin_type: composite
steps:
  - name: retrieve
    plugin: knowledge_search
    inputs:
      query: "{{state.user_query}}"
    outputs:
      context: "{{result.data}}"
  - name: generate
    plugin: llm_call
    inputs:
      messages:
        - role: user
          content: "{{state.context}}"
"#;
        let config = CompositePluginYaml::from_yaml_str(yaml).unwrap();
        assert_eq!(config.id, "rag_generator");
        assert_eq!(config.plugin_type, "composite");
        assert_eq!(config.steps.len(), 2);
        assert_eq!(config.steps[0].plugin, "knowledge_search");
        assert_eq!(config.steps[1].plugin, "llm_call");
    }

    #[test]
    fn test_composite_plugin_wrong_type() {
        let yaml = r#"
id: test
name: Test
version: "1.0.0"
plugin_type: pipeline
steps: []
"#;
        let result = CompositePluginYaml::from_yaml_str(yaml);
        assert!(result.is_err());
    }

    #[test]
    fn test_composite_plugin_validate_vars() {
        let yaml = r#"
id: test
name: Test
version: "1.0.0"
plugin_type: composite
steps:
  - name: step1
    plugin: plugin_a
    inputs:
      query: "{{state.user_query}}"
"#;
        let config = CompositePluginYaml::from_yaml_str(yaml).unwrap();
        assert!(config.validate_step_vars().is_ok());
    }

    #[test]
    fn test_yaml_anchor_alias_merge() {
        let yaml = r#"
base: &base
  timeout: 30
  retries: 3
service1:
  <<: *base
  name: svc1
service2:
  <<: *base
  name: svc2
"#;
        let loader = ConfigLoader::new("/tmp", None);
        let result = loader.parse_yaml(yaml, "test").unwrap();
        assert_eq!(result["service1"]["timeout"], 30);
        assert_eq!(result["service1"]["retries"], 3);
        assert_eq!(result["service1"]["name"], "svc1");
        assert_eq!(result["service2"]["timeout"], 30);
        assert_eq!(result["service2"]["name"], "svc2");
    }

    #[test]
    fn test_path_ref_single_file() {
        let temp = tempfile::tempdir().unwrap();
        let project_root = temp.path();
        let config_dir = project_root.join("config");
        fs::create_dir(&config_dir).unwrap();
        let rules_file = project_root.join("rules.md");
        fs::write(&rules_file, "# Rule 1\nDo good.\n").unwrap();

        let yaml = "content: '{{path:rules.md}}'";
        let loader = ConfigLoader::new(&config_dir, None);
        let result = loader.parse_yaml(yaml, "test").unwrap();
        assert!(result["content"].as_str().unwrap().contains("Rule 1"));
    }

    #[test]
    fn test_path_ref_missing_returns_error() {
        // AC-03-3: 路径不存在时应返回错误而非静默空串
        let temp = tempfile::tempdir().unwrap();
        let config_dir = temp.path().join("config");
        fs::create_dir(&config_dir).unwrap();

        let yaml = "content: '{{path:nonexistent.md}}'";
        let loader = ConfigLoader::new(&config_dir, None);
        let result = loader.parse_yaml(yaml, "test");
        assert!(result.is_err());
    }

    #[test]
    fn test_load_all_includes_yml_extension() {
        // AC-03-1: load_all 应同时匹配 .yaml 和 .yml
        let temp = tempfile::tempdir().unwrap();
        fs::write(temp.path().join("a.yaml"), "key: value_a\n").unwrap();
        fs::write(temp.path().join("b.yml"), "key: value_b\n").unwrap();

        let loader = ConfigLoader::new(temp.path(), None);
        let result = loader.load_all().unwrap();
        assert!(result.contains_key("a"));
        assert!(result.contains_key("b"));
    }

    #[test]
    fn test_should_include_file_extensions() {
        // 验证 {{path:dir|extensions=.md,.yaml}} 语法正确解析
        let md_file = Path::new("/tmp/test.md");
        let yaml_file = Path::new("/tmp/test.yaml");
        let txt_file = Path::new("/tmp/test.txt");

        assert!(should_include_file(md_file, Some(".md,.yaml")));
        assert!(should_include_file(yaml_file, Some(".md,.yaml")));
        assert!(!should_include_file(txt_file, Some(".md,.yaml")));
        assert!(should_include_file(txt_file, None)); // 无过滤，全包含
    }
}
