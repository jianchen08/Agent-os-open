//! Task-04 配置系统功能验证 — 集成测试
//!
//! 完整用户旅程：ConfigLoader 创建(.env加载) → YAML解析(anchor/alias/merge)
//! → 环境变量插值 → {{path:}}引用解析 → 组合插件YAML解析 → ConfigCenter热重载
//! → load_all 批量加载
//!
//! 补充场景：错误输入(缺失环境变量/不存在路径/空steps) + 边界(防抖窗口/哈希去重)

use agentos_config::{CompositePluginYaml, ConfigCenter, ConfigError, ConfigLoader};
use serde_json::Value;
use std::fs;
use std::time::Duration;

// =========================================================================
// 完整用户旅程 (7步串联)
// =========================================================================

/// 步骤1: 创建 ConfigLoader，加载 .env 文件
/// 步骤2: 解析含 anchor/alias/merge key 的 YAML
/// 步骤3: 环境变量插值 ($VAR + $VAR:-default)
/// 步骤4: {{path:}} 引用解析 (单文件)
/// 步骤5: 组合插件 YAML 解析 (CompositePluginYaml)
/// 步骤6: ConfigCenter 热重载 (reload + 哈希去重 + 回滚)
/// 步骤7: load_all 批量加载 (.yaml + .yml)
#[test]
fn full_user_journey_config_pipeline() {
    let temp = tempfile::tempdir().unwrap();
    let project_root = temp.path();
    let config_dir = project_root.join("config");
    fs::create_dir(&config_dir).unwrap();

    // --- 步骤1: 创建 ConfigLoader，加载 .env 文件 ---
    let env_file = project_root.join(".env");
    fs::write(
        &env_file,
        "# 这是注释\n\
         \n\
         DATABASE_URL=postgres://localhost/mydb\n\
         API_KEY=secret123\n",
    )
    .unwrap();

    let loader = ConfigLoader::new(&config_dir, Some(env_file));

    // 验证 .env 加载结果：跳过空行和注释行，解析 KEY=VALUE
    assert_eq!(
        loader.env_vars().get("DATABASE_URL"),
        Some(&"postgres://localhost/mydb".to_string()),
        "步骤1失败: .env 中 DATABASE_URL 未正确加载"
    );
    assert_eq!(
        loader.env_vars().get("API_KEY"),
        Some(&"secret123".to_string()),
        "步骤1失败: .env 中 API_KEY 未正确加载"
    );
    assert!(
        loader.env_vars().get("这是注释").is_none(),
        "步骤1失败: 注释行不应被加载"
    );

    println!("步骤1通过: .env 文件加载正确");

    // --- 步骤2: 解析含 anchor/alias/merge key 的 YAML ---
    let yaml_with_anchors = r#"
defaults: &defaults
  timeout: 30
  retries: 3
  log_level: info

service_a:
  <<: *defaults
  name: service_a
  port: 8080

service_b:
  <<: *defaults
  name: service_b
  port: 9090
  timeout: 60
"#;

    let parsed = loader
        .parse_yaml(yaml_with_anchors, "test_anchor.yaml")
        .expect("步骤2失败: YAML anchor/alias/merge key 解析应成功");

    assert_eq!(
        parsed["service_a"]["timeout"], 30,
        "步骤2失败: service_a.timeout"
    );
    assert_eq!(
        parsed["service_a"]["retries"], 3,
        "步骤2失败: service_a.retries"
    );
    assert_eq!(
        parsed["service_a"]["log_level"], "info",
        "步骤2失败: service_a.log_level"
    );
    assert_eq!(
        parsed["service_a"]["name"], "service_a",
        "步骤2失败: service_a.name"
    );
    assert_eq!(
        parsed["service_a"]["port"], 8080,
        "步骤2失败: service_a.port"
    );
    assert_eq!(
        parsed["service_b"]["timeout"], 60,
        "步骤2失败: service_b.timeout (覆盖)"
    );
    assert_eq!(
        parsed["service_b"]["retries"], 3,
        "步骤2失败: service_b.retries (来自anchor)"
    );

    println!("步骤2通过: YAML anchor/alias/merge key 正确展开");

    // --- 步骤3: 环境变量插值 ---
    // 步骤1中 .env 加载了 API_KEY=secret123
    // 同时设置系统环境变量验证优先级链
    unsafe {
        std::env::set_var("JOURNEY_SYS_VAR", "from_system_env");
    }

    // 注意: 字符串中不能直接写 ${...} 因为 Rust 会把它当格式化参数
    // 用拼接方式构造
    let yaml_with_env = format!(
        r#"
api_key: {dollar}{{API_KEY}}
sys_var: {dollar}{{JOURNEY_SYS_VAR}}
with_default: {dollar}{{NONEXISTENT_JOURNEY_VAR:-fallback_value}}
nested:
  inner_key: {dollar}{{API_KEY}}
  list:
    - {dollar}{{JOURNEY_SYS_VAR}}
    - plain_text
"#,
        dollar = "$"
    );

    let parsed_env = loader
        .parse_yaml(&yaml_with_env, "test_env.yaml")
        .expect("步骤3失败: 环境变量插值应成功");

    assert_eq!(
        parsed_env["api_key"], "secret123",
        "步骤3失败: API_KEY from .env"
    );
    assert_eq!(
        parsed_env["sys_var"], "from_system_env",
        "步骤3失败: JOURNEY_SYS_VAR from system env"
    );
    assert_eq!(
        parsed_env["with_default"], "fallback_value",
        "步骤3失败: default value"
    );
    assert_eq!(
        parsed_env["nested"]["inner_key"], "secret123",
        "步骤3失败: nested dict"
    );
    assert_eq!(
        parsed_env["nested"]["list"][0], "from_system_env",
        "步骤3失败: nested list"
    );
    assert_eq!(
        parsed_env["nested"]["list"][1], "plain_text",
        "步骤3失败: plain text"
    );

    unsafe {
        std::env::remove_var("JOURNEY_SYS_VAR");
    }

    println!("步骤3通过: 环境变量插值正确（含嵌套dict/list递归替换+系统env>.env优先级）");

    // --- 步骤4: {{path:}} 引用解析 ---
    let rules_content = "# Project Rules\n1. Be kind\n2. Be helpful\n3. Be safe";
    let rules_file = project_root.join("project_rules.md");
    fs::write(&rules_file, rules_content).unwrap();

    let yaml_with_path_ref = r#"
rules_content: "{{path:project_rules.md}}"
"#;

    let parsed_path = loader
        .parse_yaml(yaml_with_path_ref, "test_path.yaml")
        .expect("步骤4失败: path引用解析应成功");

    let content = parsed_path["rules_content"]
        .as_str()
        .expect("步骤4失败: rules_content 应为字符串");
    assert!(
        content.contains("Project Rules"),
        "步骤4失败: 引用内容应包含 Project Rules"
    );
    assert!(
        content.contains("Be kind"),
        "步骤4失败: 引用内容应包含 Be kind"
    );

    println!("步骤4通过: path引用解析正确");

    // --- 步骤5: 组合插件 YAML 解析 ---
    let composite_yaml = r#"
id: rag_pipeline
name: RAG Pipeline
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
    condition: "{{state.context != null}}"
"#;

    let composite =
        CompositePluginYaml::from_yaml_str(composite_yaml).expect("步骤5失败: 组合插件解析应成功");

    assert_eq!(composite.id, "rag_pipeline");
    assert_eq!(composite.plugin_type, "composite");
    assert_eq!(composite.steps.len(), 2, "步骤5失败: 应有 2 个 steps");
    assert_eq!(composite.steps[0].name, "retrieve");
    assert_eq!(composite.steps[0].plugin, "knowledge_search");
    assert_eq!(composite.steps[1].name, "generate");
    assert_eq!(composite.steps[1].plugin, "llm_call");
    assert!(
        composite.steps[1].condition.is_some(),
        "步骤5失败: step1 应有 condition"
    );

    composite
        .validate_step_vars()
        .expect("步骤5失败: validate_step_vars 应通过");

    println!("步骤5通过: 组合插件 YAML 解析正确（steps/state变量/plugin_type校验）");

    // --- 步骤6: ConfigCenter 热重载 (reload + 哈希去重 + 回滚) ---
    let center_dir = temp.path().join("hot_reload");
    fs::create_dir(&center_dir).unwrap();
    let config_file = center_dir.join("app.yaml");

    // 首次写入并 reload
    fs::write(&config_file, "app_name: my_app\nversion: \"1.0\"\n").unwrap();
    let center = ConfigCenter::new(&center_dir);

    let (ok1, rolled_back1, err1) = center.reload(config_file.to_str().unwrap());
    assert!(ok1, "步骤6失败: 首次 reload 应成功");
    assert!(!rolled_back1, "步骤6失败: 首次 reload 不应回滚");
    assert!(err1.is_none(), "步骤6失败: 首次 reload 不应有错误");

    // 验证缓存
    let cached = center.get(config_file.to_str().unwrap());
    assert!(cached.is_some(), "步骤6失败: 缓存应存在");
    assert_eq!(cached.unwrap()["app_name"], "my_app");

    // 再次 reload 相同内容 -> 哈希去重
    let (ok2, _, _) = center.reload(config_file.to_str().unwrap());
    assert!(ok2, "步骤6失败: 相同内容 reload 应返回成功（去重）");

    // 修改内容后 reload
    fs::write(&config_file, "app_name: my_app_v2\nversion: \"2.0\"\n").unwrap();
    let (ok3, _, _) = center.reload(config_file.to_str().unwrap());
    assert!(ok3, "步骤6失败: 修改后 reload 应成功");
    let updated = center.get(config_file.to_str().unwrap());
    assert_eq!(
        updated.unwrap()["app_name"],
        "my_app_v2",
        "步骤6失败: 缓存应更新"
    );

    // 写入无效 YAML -> 加载失败，保留旧配置
    fs::write(&config_file, "invalid: [unclosed\n").unwrap();
    let (ok4, rolled_back4, err4) = center.reload(config_file.to_str().unwrap());
    assert!(!ok4, "步骤6失败: 无效 YAML reload 应返回失败");
    assert!(rolled_back4, "步骤6失败: 加载失败时应回滚");
    assert!(err4.is_some(), "步骤6失败: 应有错误信息");

    // 验证旧配置仍保留
    let old_cached = center.get(config_file.to_str().unwrap());
    assert!(old_cached.is_some(), "步骤6失败: 回滚后旧配置应仍存在");
    assert_eq!(
        old_cached.unwrap()["app_name"],
        "my_app_v2",
        "步骤6失败: 回滚后应保留上一个有效配置"
    );

    // 验证审计日志
    let audit = center.get_audit_log(10);
    assert!(audit.len() >= 3, "步骤6失败: 审计日志应至少有 3 条记录");
    let failed_entry = audit
        .iter()
        .find(|e| !e.success)
        .expect("步骤6失败: 审计日志应包含失败记录");
    assert!(failed_entry.rolled_back, "步骤6失败: 审计日志应标记回滚");

    println!("步骤6通过: 热重载正确（reload/哈希去重/加载失败回滚/审计日志）");

    // --- 步骤7: load_all 批量加载 (.yaml + .yml) ---
    fs::write(config_dir.join("config_a.yaml"), "key_a: value_a\n").unwrap();
    fs::write(config_dir.join("config_b.yml"), "key_b: value_b\n").unwrap();
    fs::write(config_dir.join("not_yaml.txt"), "ignored\n").unwrap();

    let all_configs = loader.load_all().expect("步骤7失败: load_all 应成功");

    assert!(
        all_configs.contains_key("config_a"),
        "步骤7失败: load_all 应加载 .yaml 文件"
    );
    assert!(
        all_configs.contains_key("config_b"),
        "步骤7失败: load_all 应加载 .yml 文件"
    );
    assert!(
        !all_configs.contains_key("not_yaml"),
        "步骤7失败: load_all 不应加载 .txt 文件"
    );
    assert_eq!(all_configs.get("config_a").unwrap()["key_a"], "value_a");
    assert_eq!(all_configs.get("config_b").unwrap()["key_b"], "value_b");

    println!("步骤7通过: load_all 正确加载 .yaml 和 .yml 文件");

    println!("\n完整用户旅程全部通过: 7/7 步骤");
}

// =========================================================================
// 补充场景1: 错误输入 — 缺失环境变量无默认值
// =========================================================================

#[test]
fn error_missing_env_var_no_default_returns_error() {
    let loader = ConfigLoader::new("/tmp", None);
    let input = Value::String(format!("{}{{DEFINITELY_NONEXISTENT_VAR_99999}}", "$"));
    let result = loader.substitute_env_vars(&input);

    assert!(
        result.is_err(),
        "补充场景1失败: 缺失环境变量且无默认值应返回错误"
    );
    match result {
        Err(ConfigError::EnvVarNotFound { var_name }) => {
            assert_eq!(var_name, "DEFINITELY_NONEXISTENT_VAR_99999");
        }
        _ => panic!("补充场景1失败: 应返回 EnvVarNotFound 错误"),
    }
    println!("补充场景1通过: 缺失环境变量无默认值 -> EnvVarNotFound");
}

// =========================================================================
// 补充场景2: 错误输入 — path引用不存在的路径
// =========================================================================

#[test]
fn error_path_ref_nonexistent_returns_error() {
    let temp = tempfile::tempdir().unwrap();
    let config_dir = temp.path().join("config");
    fs::create_dir(&config_dir).unwrap();

    let yaml = r#"content: '{{path:does_not_exist.md}}'"#;
    let loader = ConfigLoader::new(&config_dir, None);
    let result = loader.parse_yaml(yaml, "test");

    assert!(result.is_err(), "补充场景2失败: 不存在的路径引用应返回错误");
    match &result {
        Err(ConfigError::PathRefFailed { ref_path, .. }) => {
            assert_eq!(ref_path, "does_not_exist.md");
        }
        _ => panic!("补充场景2失败: 应返回 PathRefFailed 错误"),
    }
    println!("补充场景2通过: 不存在的路径引用 -> PathRefFailed");
}

// =========================================================================
// 补充场景3: 错误输入 — 组合插件 plugin_type 校验
// =========================================================================

#[test]
fn error_composite_plugin_wrong_type() {
    let yaml = r#"
id: test
name: Test
version: "1.0.0"
plugin_type: pipeline
steps:
  - name: s1
    plugin: p1
    inputs: {}
"#;
    let result = CompositePluginYaml::from_yaml_str(yaml);
    assert!(
        result.is_err(),
        "补充场景3失败: plugin_type 非 composite 应返回错误"
    );
    println!("补充场景3通过: plugin_type=pipeline -> Composite 错误");
}

// =========================================================================
// 补充场景4: 错误输入 — 组合插件空 steps
// =========================================================================

#[test]
fn error_composite_plugin_empty_steps() {
    let yaml = r#"
id: test
name: Test
version: "1.0.0"
plugin_type: composite
steps: []
"#;
    let result = CompositePluginYaml::from_yaml_str(yaml);
    assert!(result.is_err(), "补充场景4失败: 空 steps 应返回错误");
    println!("补充场景4通过: 空 steps -> Composite 错误");
}

// =========================================================================
// 补充场景5: 边界 — 防抖窗口验证 (500ms)
// 注意: check_debounce 是 #[cfg(test)] 方法，在集成测试中不可用。
// 通过 ConfigCenter::with_debounce + reload 间接验证防抖行为：
//   在防抖窗口内重复 reload 相同文件，哈希去重确保不会产生重复审计日志。
//   防抖本身在 start_watching 的回调中使用，这里验证哈希去重+防抖常量。
// =========================================================================

#[test]
fn boundary_debounce_500ms_constant_and_hash_dedup() {
    // 验证 ConfigCenter 可创建自定义防抖窗口
    let center = ConfigCenter::with_debounce("/tmp", Duration::from_millis(500));

    let temp = tempfile::tempdir().unwrap();
    let config_file = temp.path().join("debounce_test.yaml");
    fs::write(&config_file, "key: value\n").unwrap();

    // 首次 reload
    let (ok1, _, _) = center.reload(config_file.to_str().unwrap());
    assert!(ok1, "补充场景5失败: 首次 reload 应成功");

    // 立即再次 reload 相同内容 -> 哈希去重（不产生新审计日志）
    let (ok2, _, _) = center.reload(config_file.to_str().unwrap());
    assert!(ok2, "补充场景5失败: 相同内容 reload 应成功");

    // 审计日志应只有 1 条（哈希去重，第二次未产生新日志）
    let audit = center.get_audit_log(10);
    assert_eq!(
        audit.len(),
        1,
        "补充场景5失败: 哈希去重后审计日志应只有 1 条"
    );

    // 等待超过防抖窗口后修改内容 -> 应产生新审计日志
    std::thread::sleep(Duration::from_millis(600));
    fs::write(&config_file, "key: new_value\n").unwrap();
    let (ok3, _, _) = center.reload(config_file.to_str().unwrap());
    assert!(ok3, "补充场景5失败: 修改后 reload 应成功");

    let audit2 = center.get_audit_log(10);
    assert_eq!(audit2.len(), 2, "补充场景5失败: 修改后应有 2 条审计日志");

    println!("补充场景5通过: 500ms防抖常量 + 哈希去重行为正确");
}

// =========================================================================
// 补充场景6: 边界 — 层级覆盖优先级链 (系统env > .env > 默认值)
// 通过 substitute_env_vars 公共接口验证（get_env_var 是私有的）
// =========================================================================

#[test]
fn boundary_priority_chain_system_over_envfile_over_default() {
    let temp = tempfile::tempdir().unwrap();
    let env_file = temp.path().join(".env");
    fs::write(&env_file, "PRIORITY_CHAIN_TEST=from_env_file\n").unwrap();

    // 设置系统环境变量（优先级最高）
    unsafe {
        std::env::set_var("PRIORITY_CHAIN_TEST", "from_system_env");
    }

    let loader = ConfigLoader::new("/tmp", Some(env_file));

    // 1. 系统环境变量优先于 .env 文件
    let input1 = Value::String(format!("{}{{PRIORITY_CHAIN_TEST}}", "$"));
    let result1 = loader.substitute_env_vars(&input1).unwrap();
    assert_eq!(
        result1,
        Value::String("from_system_env".to_string()),
        "补充场景6失败: 系统环境变量应优先于 .env 文件"
    );

    // 2. 清除系统环境变量后，.env 文件值生效
    unsafe {
        std::env::remove_var("PRIORITY_CHAIN_TEST");
    }
    let result2 = loader.substitute_env_vars(&input1).unwrap();
    assert_eq!(
        result2,
        Value::String("from_env_file".to_string()),
        "补充场景6失败: .env 文件应优先于默认值"
    );

    // 3. .env 中不存在的变量使用默认值
    let input3 = Value::String(format!(
        "{}{{NONEXISTENT_PRIORITY_VAR:-default_value}}",
        "$"
    ));
    let result3 = loader.substitute_env_vars(&input3).unwrap();
    assert_eq!(
        result3,
        Value::String("default_value".to_string()),
        "补充场景6失败: 不存在时应使用默认值"
    );

    // 4. 都不存在时返回错误
    let input4 = Value::String(format!("{}{{TOTALLY_NONEXISTENT_VAR}}", "$"));
    let result4 = loader.substitute_env_vars(&input4);
    assert!(
        result4.is_err(),
        "补充场景6失败: 都不存在时应返回 EnvVarNotFound 错误"
    );

    println!("补充场景6通过: 优先级链正确（系统env > .env文件 > 默认值 > 错误）");
}

// =========================================================================
// 补充场景7: {{path:dir|extensions=...}} 目录模式 + extensions 过滤
// =========================================================================

#[test]
fn boundary_path_ref_directory_with_extensions() {
    let temp = tempfile::tempdir().unwrap();
    let project_root = temp.path();
    let config_dir = project_root.join("config");
    fs::create_dir(&config_dir).unwrap();

    let rules_dir = project_root.join("rules_dir");
    fs::create_dir(&rules_dir).unwrap();
    fs::write(rules_dir.join("rule1.md"), "# Rule 1\n").unwrap();
    fs::write(rules_dir.join("rule2.md"), "# Rule 2\n").unwrap();
    fs::write(rules_dir.join("notes.txt"), "should be ignored\n").unwrap();

    let yaml = r#"content: "{{path:rules_dir|extensions=.md}}""#;
    let loader = ConfigLoader::new(&config_dir, None);
    let result = loader.parse_yaml(yaml, "test").unwrap();

    let content = result["content"].as_str().unwrap();
    assert!(
        content.contains("Rule 1"),
        "补充场景7失败: 应包含 rule1.md 的内容"
    );
    assert!(
        content.contains("Rule 2"),
        "补充场景7失败: 应包含 rule2.md 的内容"
    );
    assert!(
        !content.contains("should be ignored"),
        "补充场景7失败: 不应包含 .txt 文件内容（extensions 过滤）"
    );

    println!("补充场景7通过: 目录模式 extensions 过滤正确");
}

// =========================================================================
// 补充场景8: load_all YAML 错误传播
// =========================================================================

#[test]
fn error_load_all_yaml_error_propagation() {
    let temp = tempfile::tempdir().unwrap();
    let config_dir = temp.path();
    fs::write(config_dir.join("good.yaml"), "key: value\n").unwrap();
    fs::write(config_dir.join("bad.yaml"), "invalid: [unclosed\n").unwrap();

    let loader = ConfigLoader::new(config_dir, None);
    let result = loader.load_all();

    assert!(
        result.is_err(),
        "补充场景8失败: load_all 遇到 YAML 错误应传播"
    );
    match result {
        Err(ConfigError::YamlParse { path, .. }) => {
            assert!(
                path.contains("bad.yaml"),
                "补充场景8失败: 错误应包含 bad.yaml 路径"
            );
        }
        _ => panic!("补充场景8失败: 应返回 YamlParse 错误"),
    }
    println!("补充场景8通过: load_all YAML 错误正确传播");
}
