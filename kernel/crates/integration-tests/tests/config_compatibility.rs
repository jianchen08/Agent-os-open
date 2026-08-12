//! Task-12 配置兼容性对比测试
//!
//! 验证 Rust `serde_yaml` 与 Python `yaml.safe_load` 在解析同一 YAML 文件时
//! 产出等价的 JSON 结果——确保 0.1 的配置文件零修改可被 0.2 Rust 内核加载。
//!
//! 测试策略：
//! 1. 用 ConfigLoader 加载代表性 YAML 配置（覆盖 anchor/alias/merge key）
//! 2. 验证解析结果结构正确
//! 3. 验证环境变量插值与 Python 行为一致
//! 4. 验证边界场景（空文件、嵌套、数字/布尔类型推断）
//!
//! 对应 AC-11-3（traces_to: AC-4）
//!
//! @feature: FP-0.2.CFG 配置系统与插件配置注入 | @vision: V3 可嵌入 | @ci: rust-test

use agentos_config::ConfigLoader;
use serde_json::{json, Value};

// ═══════════════════════════════════════════════════════════════════
// 测试 1: 基础 YAML 结构——键值/列表/嵌套
// ═══════════════════════════════════════════════════════════════════

#[test]
fn test_basic_yaml_structure_equivalence() {
    let yaml = r#"
name: test_agent
version: "1.0"
enabled: true
count: 42
tags:
  - production
  - stable
config:
  timeout: 30
  retries: 3
"#;

    let loader = ConfigLoader::new("/tmp/nonexistent", None);
    let result = loader.parse_yaml(yaml, "test").unwrap();

    // Python yaml.safe_load 会将这些解析为对应类型
    // Rust serde_yaml + serde_json 也应该保持一致
    assert_eq!(result["name"], json!("test_agent"));
    assert_eq!(result["version"], json!("1.0"));
    assert_eq!(result["enabled"], json!(true));
    assert_eq!(result["count"], json!(42));
    assert_eq!(result["tags"], json!(["production", "stable"]));
    assert_eq!(result["config"]["timeout"], json!(30));
    assert_eq!(result["config"]["retries"], json!(3));
}

// ═══════════════════════════════════════════════════════════════════
// 测试 2: YAML anchor/alias 等价性
// ═══════════════════════════════════════════════════════════════════

#[test]
fn test_yaml_anchor_alias_equivalence() {
    let yaml = r#"
defaults: &defaults
  timeout: 30
  retries: 3
  enabled: true

agent_a:
  <<: *defaults
  name: agent_a

agent_b:
  <<: *defaults
  name: agent_b
  timeout: 60
"#;

    let loader = ConfigLoader::new("/tmp/nonexistent", None);
    let result = loader.parse_yaml(yaml, "test").unwrap();

    // Python yaml.safe_load 展开 merge key 后的行为
    let agent_a = &result["agent_a"];
    assert_eq!(agent_a["name"], json!("agent_a"));
    assert_eq!(agent_a["timeout"], json!(30)); // 继承自 defaults
    assert_eq!(agent_a["retries"], json!(3));
    assert_eq!(agent_a["enabled"], json!(true));

    let agent_b = &result["agent_b"];
    assert_eq!(agent_b["name"], json!("agent_b"));
    assert_eq!(agent_b["timeout"], json!(60)); // 覆盖 defaults
    assert_eq!(agent_b["retries"], json!(3)); // 继承自 defaults
}

// ═══════════════════════════════════════════════════════════════════
// 测试 3: 环境变量插值等价性
// ═══════════════════════════════════════════════════════════════════

#[test]
fn test_env_var_interpolation_equivalence() {
    // Python: os.environ.get('TEST_API_KEY', 'default_key')
    // Rust: 相同的三级优先级（系统 > .env > 默认值）
    std::env::set_var("TEST_COMPAT_KEY", "secret123");

    let yaml = r#"
api_key: ${TEST_COMPAT_KEY}
api_secret: ${TEST_COMPAT_SECRET:-fallback_secret}
default_port: ${TEST_COMPAT_PORT:-8080}
"#;

    let loader = ConfigLoader::new("/tmp/nonexistent", None);
    let result = loader.parse_yaml(yaml, "test").unwrap();

    assert_eq!(result["api_key"], json!("secret123"));
    assert_eq!(result["api_secret"], json!("fallback_secret"));
    assert_eq!(result["default_port"], json!("8080"));

    std::env::remove_var("TEST_COMPAT_KEY");
}

// ═══════════════════════════════════════════════════════════════════
// 测试 4: 数字/布尔/Null 类型推断等价性
// ═══════════════════════════════════════════════════════════════════

#[test]
fn test_type_inference_equivalence() {
    let yaml = r#"
int_val: 42
float_val: 2.5
bool_true: true
bool_false: false
null_val: null
string_val: "hello"
quoted_number: "42"
empty_string: ""
"#;

    let loader = ConfigLoader::new("/tmp/nonexistent", None);
    let result = loader.parse_yaml(yaml, "test").unwrap();

    // Python yaml.safe_load 对这些的类型推断结果
    assert_eq!(result["int_val"], json!(42));
    assert_eq!(result["float_val"], json!(2.5));
    assert_eq!(result["bool_true"], json!(true));
    assert_eq!(result["bool_false"], json!(false));
    assert_eq!(result["null_val"], json!(null));
    assert_eq!(result["string_val"], json!("hello"));
    assert_eq!(result["quoted_number"], json!("42"));
    assert_eq!(result["empty_string"], json!(""));
}

// ═══════════════════════════════════════════════════════════════════
// 测试 5: 深层嵌套结构等价性
// ═══════════════════════════════════════════════════════════════════

#[test]
fn test_deep_nested_structure_equivalence() {
    let yaml = r#"
pipeline:
  id: main_pipeline
  steps:
    - name: step1
      plugin: input_plugin
      inputs:
        params:
          user_id: "u123"
          roles:
            - admin
            - user
          metadata:
            created_at: "2024-01-01"
            tags:
              - priority
    - name: step2
      plugin: output_plugin
      config:
        format: json
"#;

    let loader = ConfigLoader::new("/tmp/nonexistent", None);
    let result = loader.parse_yaml(yaml, "test").unwrap();

    let step1 = &result["pipeline"]["steps"][0];
    assert_eq!(step1["name"], json!("step1"));
    assert_eq!(step1["inputs"]["params"]["user_id"], json!("u123"));
    assert_eq!(step1["inputs"]["params"]["roles"], json!(["admin", "user"]));
    assert_eq!(
        step1["inputs"]["params"]["metadata"]["tags"],
        json!(["priority"])
    );
}

// ═══════════════════════════════════════════════════════════════════
// 测试 6: 空文件和边界场景
// ═══════════════════════════════════════════════════════════════════

#[test]
fn test_empty_yaml_equivalence() {
    // Python yaml.safe_load("") returns None
    let loader = ConfigLoader::new("/tmp/nonexistent", None);
    let result = loader.parse_yaml("", "empty").unwrap();
    assert_eq!(result, json!(null));
}

#[test]
fn test_comment_only_yaml() {
    let yaml = r#"
# This is a comment
# Another comment
"#;
    let loader = ConfigLoader::new("/tmp/nonexistent", None);
    let result = loader.parse_yaml(yaml, "comments").unwrap();
    assert_eq!(result, json!(null));
}

// ═══════════════════════════════════════════════════════════════════
// 测试 7: 特殊字符处理
// ═══════════════════════════════════════════════════════════════════

#[test]
fn test_special_characters_equivalence() {
    let yaml = r#"
message: "Hello, 世界!"
special: 'Tab\there'
unicode: "emoji_test_🦀"
multiline: |
  line1
  line2
  line3
"#;

    let loader = ConfigLoader::new("/tmp/nonexistent", None);
    let result = loader.parse_yaml(yaml, "special").unwrap();

    assert_eq!(result["message"], json!("Hello, 世界!"));
    assert!(result["unicode"].as_str().unwrap().contains("🦀"));
    // multiline literal block
    let ml = result["multiline"].as_str().unwrap();
    assert!(ml.contains("line1"));
    assert!(ml.contains("line2"));
    assert!(ml.contains("line3"));
}

// ═══════════════════════════════════════════════════════════════════
// 测试 8: 组合插件 YAML（ADR ⑥）
// ═══════════════════════════════════════════════════════════════════

#[test]
fn test_composite_plugin_yaml_equivalence() {
    let yaml = r#"
id: composite_test
name: Test Composite Plugin
version: "0.1.0"
plugin_type: composite
steps:
  - name: validate
    plugin: input_validator
    inputs:
      required_fields: ["message", "session_id"]
  - name: process
    plugin: llm_core
    inputs:
      model: gpt-4
      temperature: 0.7
  - name: output
    plugin: response_formatter
    inputs:
      format: markdown
"#;

    let loader = ConfigLoader::new("/tmp/nonexistent", None);
    let result = loader.parse_yaml(yaml, "composite").unwrap();

    assert_eq!(result["id"], json!("composite_test"));
    assert_eq!(result["plugin_type"], json!("composite"));
    let steps = result["steps"].as_array().unwrap();
    assert_eq!(steps.len(), 3);
    assert_eq!(steps[1]["inputs"]["temperature"], json!(0.7));
}

// ═══════════════════════════════════════════════════════════════════
// 测试 9: 真实配置文件结构模拟（agents/pipelines/tools）
// ═══════════════════════════════════════════════════════════════════

#[test]
fn test_realistic_agent_config_equivalence() {
    let yaml = r#"
id: coding_agent
name: Coding Agent
version: "2.0"
pipeline:
  input:
    - plugin: context_build
      inputs:
        max_history: 20
    - plugin: prompt_build
      inputs:
        system_prompt: "You are a coding assistant."
  core:
    - plugin: llm_call
      inputs:
        model: gpt-4
        max_tokens: 4096
  output:
    - plugin: route_arbiter
      inputs:
        rules:
          - condition: "has_tool_calls"
            route: next_tool
          - condition: "default"
            route: next_llm
tools:
  - name: code_search
    description: "Search code in the project"
    category: search
  - name: file_edit
    description: "Edit a file"
    category: file
"#;

    let loader = ConfigLoader::new("/tmp/nonexistent", None);
    let result = loader.parse_yaml(yaml, "agent").unwrap();

    assert_eq!(result["id"], json!("coding_agent"));
    assert_eq!(result["pipeline"]["input"].as_array().unwrap().len(), 2);
    assert_eq!(
        result["pipeline"]["core"][0]["inputs"]["model"],
        json!("gpt-4")
    );
    assert_eq!(result["tools"].as_array().unwrap().len(), 2);
}

// ═══════════════════════════════════════════════════════════════════
// 测试 10: JSON 序列化往返一致性
// ═══════════════════════════════════════════════════════════════════

#[test]
fn test_json_roundtrip_consistency() {
    let yaml = r#"
name: roundtrip_test
nested:
  key1: value1
  key2: 42
  key3: true
list:
  - item1
  - item2
"#;

    let loader = ConfigLoader::new("/tmp/nonexistent", None);
    let parsed = loader.parse_yaml(yaml, "roundtrip").unwrap();

    // 序列化为 JSON 字符串
    let json_str = serde_json::to_string(&parsed).unwrap();
    // 反序列化回来
    let roundtrip: Value = serde_json::from_str(&json_str).unwrap();

    assert_eq!(parsed, roundtrip, "JSON roundtrip should be lossless");
}

// ═══════════════════════════════════════════════════════════════════
// 测试 11: 文件加载→JSON→深度比较
// ═══════════════════════════════════════════════════════════════════

#[test]
fn test_file_load_to_json_comparison() {
    let temp_dir = tempfile::tempdir().unwrap();
    let yaml_content = r#"
id: file_test_agent
name: File Test Agent
version: "1.0"
config:
  feature_flags:
    enable_cache: true
    max_cache_size: 1000
  endpoints:
    - /api/v1/chat
    - /api/v1/schema
"#;
    std::fs::write(temp_dir.path().join("agent.yaml"), yaml_content).unwrap();

    let loader = ConfigLoader::new(temp_dir.path(), None);
    let config = loader.load_yaml("agent.yaml").unwrap();

    // 模拟 Python 加载后转 JSON 的结果
    let expected = json!({
        "id": "file_test_agent",
        "name": "File Test Agent",
        "version": "1.0",
        "config": {
            "feature_flags": {
                "enable_cache": true,
                "max_cache_size": 1000
            },
            "endpoints": ["/api/v1/chat", "/api/v1/schema"]
        }
    });

    // 深度比较
    assert_eq!(
        config, expected,
        "Rust loaded config should match expected JSON"
    );
}
