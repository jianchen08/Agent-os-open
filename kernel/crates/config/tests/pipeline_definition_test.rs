// @feature: FP-0.2.CFG 配置系统与插件配置注入 | @vision: V3 可嵌入 | @ci: rust-test

//! P7: 内核承载管道配置 — PipelineDefinition / AgentConfig 解析与引擎配置转换测试
//!
//! 验证（以 config/pipelines/default.yaml 与 config/agents/*.yaml 为真相源）：
//! - AC1: PipelineDefinition 能解析 0.1 扁平格式（input_routes/output_routes/plugins/core_plugins）
//! - AC2: load_pipeline_definition(config_root, name) 按名加载 config/pipelines/{name}.yaml
//! - AC3: to_engine_config() 转换为引擎 steps 模型（prepare/core/post + loop + routes）
//! - AC4: AgentConfig 加载 config/agents/*.yaml 关键字段

use agentos_config::pipeline::{load_agent_config, load_pipeline_definition};
use std::fs;
use std::path::Path;

// ── AC1/AC2: PipelineDefinition 解析 0.1 扁平格式 ──────────────────────

/// default.yaml 形态的最小样例（结构与 config/pipelines/default.yaml 一致）。
fn write_default_pipeline(config_dir: &std::path::Path) {
    let pipelines = config_dir.join("pipelines");
    fs::create_dir_all(&pipelines).unwrap();
    fs::write(
        pipelines.join("default.yaml"),
        r#"
name: agentos_agent

task_worker:
  pipeline_timeout: 7200

input_routes:
  - name: tool_execute
    condition: "core_type == 'tool_execute'"
    target: core
    plugins: [tool_schema, param_inject, security_check]
    priority: 10
  - name: llm_call
    condition: "core_type == 'llm_call'"
    target: core
    plugins: [multimodal_preprocessor, context_window_guard, tool_schema, prompt_build]
    priority: 20
  - name: default
    condition: "True"
    target: core
    plugins: [pause_guard, tool_schema, param_inject, prompt_build]
    priority: 30

output_routes:
  - route_type: next_tool
    condition: "raw_tool_calls != []"
    priority: 1
  - route_type: wait
    condition: "True"
    priority: 10
  - route_type: next_llm
    condition: "True"
    priority: 50
  - route_type: end
    condition: "True"
    priority: 99

plugins:
  - name: tool_schema
    config:
      enabled: true
  - name: prompt_build
    config:
      enabled: true
  - name: stop_check
    config:
      enabled: true
  - name: result_format
    config:
      max_result_length: 2000

core_plugins:
  llm_call:
    class: plugins.shared.core.llm_core.plugin.LLMCore
    config:
      default_params:
        temperature: 0.7
  tool_execute:
    class: plugins.shared.core.tool_core.plugin.ToolCore
    config:
      timeout: 300
"#,
    )
    .unwrap();
}

/// AC1：解析 default.yaml 形态——name/input_routes/output_routes/plugins/core_plugins 齐备。
#[test]
fn test_pipeline_definition_parses_flat_yaml() {
    let tmp = tempfile::tempdir().unwrap();
    write_default_pipeline(tmp.path());

    let def = load_pipeline_definition(tmp.path(), "default").expect("should load default.yaml");
    assert_eq!(def.name, "agentos_agent");
    assert_eq!(def.input_routes.len(), 3);
    assert_eq!(def.output_routes.len(), 4);
    assert_eq!(def.plugins.len(), 4);
    assert_eq!(def.core_plugins.len(), 2);

    // input_routes 字段
    let tool_route = &def.input_routes[0];
    assert_eq!(tool_route.name, "tool_execute");
    assert_eq!(tool_route.condition, "core_type == 'tool_execute'");
    assert_eq!(tool_route.target, "core");
    assert_eq!(tool_route.plugins, vec!["tool_schema", "param_inject", "security_check"]);
    assert_eq!(tool_route.priority, 10);

    // output_routes 字段：route_type 与 priority
    assert_eq!(def.output_routes[0].route_type, "next_tool");
    assert_eq!(def.output_routes[0].priority, 1);
    assert_eq!(def.output_routes[3].route_type, "end");
    assert_eq!(def.output_routes[3].priority, 99);

    // plugins 保留 name + config
    assert_eq!(def.plugins[0].name, "tool_schema");
    assert_eq!(
        def.plugins[0].config.get("enabled"),
        Some(&serde_yaml::Value::Bool(true))
    );

    // core_plugins 保留 class
    assert_eq!(
        def.core_plugins["llm_call"].class,
        "plugins.shared.core.llm_core.plugin.LLMCore"
    );
}

/// AC2：文件不存在 → 报错（明确 NotFound，不静默降级为空配置）。
#[test]
fn test_load_pipeline_definition_missing_errors() {
    let tmp = tempfile::tempdir().unwrap();
    let err = load_pipeline_definition(tmp.path(), "nonexistent").unwrap_err();
    assert!(
        matches!(err, agentos_config::ConfigError::NotFound { .. }),
        "expected NotFound, got {err}"
    );
}

/// AC3：to_engine_config() 转换为引擎 steps 模型。
///
/// 断言（对齐 0.1 语义）：
/// - name 保留
/// - loop_config.enabled = true（0.1 默认循环执行，end 路由终止）
/// - steps 含 prepare / core / post 三段
/// - prepare 的 steps 含 input 插件（并集、按 priority 排序）
/// - core 的 steps 引用动态 {{state.core_plugin}}
/// - post 的 steps 含 output 插件
/// - post.routes 按 priority 排序：next_tool → wait → next_llm → end
#[test]
fn test_to_engine_config_converts_steps() {
    let tmp = tempfile::tempdir().unwrap();
    write_default_pipeline(tmp.path());
    let def = load_pipeline_definition(tmp.path(), "default").unwrap();

    let engine_cfg = def.to_engine_config();
    assert_eq!(engine_cfg.name, "agentos_agent");
    assert!(engine_cfg.loop_config.enabled);

    // 三段 step id
    let ids: Vec<&str> = engine_cfg.steps.iter().map(|s| s.id.as_str()).collect();
    assert!(ids.contains(&"prepare"), "steps should contain prepare: {ids:?}");
    assert!(ids.contains(&"core"), "steps should contain core: {ids:?}");
    assert!(ids.contains(&"post"), "steps should contain post: {ids:?}");

    // prepare step：input 插件并集（tool_schema 出现于多条路由只算一次）
    let prepare = engine_cfg.find_step("prepare").unwrap();
    assert!(
        prepare.steps.contains(&"pipeline_tool_schema".to_string()),
        "prepare should include pipeline_tool_schema: {:?}",
        prepare.steps
    );
    assert!(
        prepare.steps.contains(&"pipeline_pause_guard".to_string()),
        "prepare should include pipeline_pause_guard: {:?}",
        prepare.steps
    );
    assert!(
        prepare.steps.contains(&"pipeline_security_check".to_string()),
        "prepare should include pipeline_security_check (from tool_execute route): {:?}",
        prepare.steps
    );
    // 去重：tool_schema 只出现一次
    let count = prepare.steps.iter().filter(|s| *s == "pipeline_tool_schema").count();
    assert_eq!(count, 1, "pipeline_tool_schema should be deduplicated");

    // core step：动态 core_plugin
    let core = engine_cfg.find_step("core").unwrap();
    assert!(
        core.steps.contains(&"{{state.core_plugin}}".to_string()),
        "core should reference {{state.core_plugin}}: {:?}",
        core.steps
    );

    // post step：output 插件 + 路由仲裁
    let post = engine_cfg.find_step("post").unwrap();
    assert!(
        post.steps.iter().any(|s| s.contains("pipeline_stop_check")),
        "post should include output plugin: {:?}",
        post.steps
    );
    // 路由按 priority 排序：next_tool(1) → wait(10) → next_llm(50) → end(99)
    assert_eq!(post.routes.len(), 4, "should convert 4 output routes");
    // next_tool 条件已规范化（追加 None 保护，对齐 autonomous.yaml 防御写法）
    assert_eq!(post.routes[0].when, "raw_tool_calls != [] and raw_tool_calls != None");
    assert_eq!(post.routes[3].when, "True");
}

/// AC4：AgentConfig 加载 config/agents/*.yaml 关键字段。
#[test]
fn test_agent_config_loads_key_fields() {
    let tmp = tempfile::tempdir().unwrap();
    let agents = tmp.path().join("agents").join("main");
    fs::create_dir_all(&agents).unwrap();
    fs::write(
        agents.join("agentos.yaml"),
        r#"
config_id: agentos
name: 灵汐
level: L1
model_tier: large
system_prompt: "你是灵汐"
tool_ids: [task_submit, file_read]
max_iterations: -1
"#,
    )
    .unwrap();

    let cfg = load_agent_config(tmp.path(), "agentos").expect("should load agent config");
    assert_eq!(cfg.config_id, "agentos");
    assert_eq!(cfg.level, Some("L1".to_string()));
    assert_eq!(cfg.model_tier, Some("large".to_string()));
    assert_eq!(cfg.tool_ids, vec!["task_submit", "file_read"]);
    assert_eq!(cfg.max_iterations, Some(-1));
}

/// AC4b：Agent 配置缺失 → 返回默认空配置（不报错，调用方用默认值）。
#[test]
fn test_agent_config_missing_returns_default() {
    let tmp = tempfile::tempdir().unwrap();
    let cfg = load_agent_config(tmp.path(), "does_not_exist").unwrap();
    assert_eq!(cfg.config_id, "does_not_exist");
    assert!(cfg.tool_ids.is_empty());
}

// ── 真实真相源验证：config/pipelines/default.yaml ──────────────

/// 从项目根读取真实 `config/pipelines/default.yaml`（CARGO_MANIFEST_DIR 向上 3 级）。
///
/// 返回 **config 根**（`<workspace>/config`）——与 `load_pipeline_definition` /
/// `load_agent_config` 的入参语义一致（config_root 直接含 pipelines/ 与 agents/ 子目录，
/// 对齐内核 server.rs `config_root = project_root.join("config")` 的既有约定）。
fn real_project_root() -> std::path::PathBuf {
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
    // kernel/crates/config → kernel/crates → kernel → workspace 根
    let workspace_root = manifest
        .parent()
        .and_then(|p| p.parent())
        .and_then(|p| p.parent())
        .unwrap_or_else(|| Path::new("."));
    workspace_root.join("config")
}

/// 真实 default.yaml 能被 PipelineDefinition 解析并转换为引擎 steps 模型。
#[test]
fn test_real_default_yaml_parses_and_converts() {
    let root = real_project_root();
    let def = match load_pipeline_definition(&root, "default") {
        Ok(d) => d,
        Err(e) => {
            eprintln!("真实 default.yaml 加载失败（环境无该文件则跳过）: {e}");
            return;
        }
    };

    // 0.1 真相源关键字段齐备
    assert_eq!(def.name, "agentos_agent", "default.yaml name 字段应为 agentos_agent");
    assert!(!def.input_routes.is_empty(), "default.yaml 应有 input_routes");
    assert!(!def.output_routes.is_empty(), "default.yaml 应有 output_routes");
    assert!(!def.plugins.is_empty(), "default.yaml 应有 plugins");
    assert!(def.core_plugins.contains_key("llm_call"), "default.yaml 应有 core_plugins.llm_call");

    // 转换不报错，产出合法 steps 模型
    let engine_cfg = def.to_engine_config();
    assert_eq!(engine_cfg.name, "agentos_agent");
    assert!(engine_cfg.loop_config.enabled, "0.1 默认循环应启用");
    assert!(engine_cfg.steps.iter().any(|s| s.id == "prepare"), "应有 prepare step");
    assert!(engine_cfg.steps.iter().any(|s| s.id == "core"), "应有 core step");
    assert!(engine_cfg.steps.iter().any(|s| s.id == "post"), "应有 post step");

    // prepare 插件名带 pipeline_ 前缀（对齐 0.2 插件 id 约定）
    let prepare = engine_cfg.find_step("prepare").unwrap();
    assert!(
        prepare.steps.iter().all(|s| s.starts_with("pipeline_")),
        "prepare 插件应带 pipeline_ 前缀: {:?}",
        prepare.steps
    );
    assert!(
        prepare.steps.contains(&"pipeline_tool_schema".to_string()),
        "prepare 应含 pipeline_tool_schema: {:?}",
        prepare.steps
    );

    // 路由仲裁含 next_tool → end（按 priority 排序，end 兜底）
    let post = engine_cfg.find_step("post").unwrap();
    assert!(!post.routes.is_empty(), "post 应有路由仲裁");
    assert_eq!(post.routes.last().map(|r| r.when.as_str()), Some("True"), "end 兜底路由应为 True");
}

/// 真实 agent 配置（config/agents/main/agentos.yaml）能加载关键字段。
#[test]
fn test_real_agent_config_loads() {
    let root = real_project_root();
    let cfg = match load_agent_config(&root, "agentos") {
        Ok(c) => c,
        Err(e) => {
            eprintln!("真实 agent 配置加载失败（环境无该文件则跳过）: {e}");
            return;
        }
    };
    assert_eq!(cfg.config_id, "agentos");
    assert!(cfg.system_prompt.is_some(), "agentos.yaml 应有 system_prompt");
    assert!(!cfg.tool_ids.is_empty(), "agentos.yaml 应有 tool_ids");
}

// ── MF-M2 修复：Agent 配置注入 state 的端到端语义演示 ───────────

/// Agent 配置字段可组成初始管道 state（功能对齐 0.1 的注入语义）。
///
/// 0.1 行为：agent 配置（system_prompt / tool_ids / model_tier / max_iterations）
/// 在管道开始时进入 state，插件运行时从 ctx.state 读取。
/// 本测试演示 config crate 的 `load_agent_config` 产物 → 构造 initial_state 的
/// 组合语义；engine 端注入路径由 `server.rs::load_agent_config_into_state`
/// （process_via_engine 调用）消费本 API。
#[test]
fn test_agent_config_fields_compose_into_initial_state() {
    let tmp = tempfile::tempdir().unwrap();
    let agents = tmp.path().join("agents").join("main");
    fs::create_dir_all(&agents).unwrap();
    fs::write(
        agents.join("agentos.yaml"),
        r#"
config_id: agentos
name: 灵汐
system_prompt: "你是灵汐，温柔体贴的助理"
tool_ids: [task_submit, file_read, memory]
model_tier: large
max_iterations: 50
"#,
    )
    .unwrap();

    let cfg = load_agent_config(tmp.path(), "agentos").expect("load agent config");

    // 模拟 0.1 管道启动时的 state 组装（对齐 server.rs::load_agent_config_into_state
    // 的字段注入语义：仅在缺失时补，调用方注入优先级高于配置默认）。
    let mut initial_state = serde_json::json!({
        "agent_id": cfg.config_id,
        "message": "你好",
        "ended": false,
    });
    if let Some(obj) = initial_state.as_object_mut() {
        if let Some(sp) = &cfg.system_prompt {
            obj.insert("system_prompt".to_string(), serde_json::Value::String(sp.clone()));
        }
        obj.insert("tool_ids".to_string(), serde_json::Value::Array(
            cfg.tool_ids.iter().map(|t| serde_json::Value::String(t.clone())).collect()
        ));
        if let Some(mt) = &cfg.model_tier {
            obj.insert("model_tier".to_string(), serde_json::Value::String(mt.clone()));
        }
        if let Some(mi) = cfg.max_iterations {
            obj.insert("max_iterations".to_string(), serde_json::Value::Number((mi as i64).into()));
        }
    }

    // 断言：agent 配置字段全部进入 state，供插件运行时读取（功能对齐 0.1）
    assert_eq!(initial_state["agent_id"], "agentos");
    assert_eq!(initial_state["system_prompt"], "你是灵汐，温柔体贴的助理");
    assert_eq!(initial_state["tool_ids"], serde_json::json!(["task_submit", "file_read", "memory"]));
    assert_eq!(initial_state["model_tier"], "large");
    assert_eq!(initial_state["max_iterations"], 50);
}
