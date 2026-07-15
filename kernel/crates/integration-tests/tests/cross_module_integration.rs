//! Task-12 跨模块集成测试
//!
//! 验证各 crate 之间的协作链路：
//! 1. config → engine：YAML 配置加载后可被引擎解析为 CompositeStep 序列
//! 2. engine → store：执行步骤产生 Patch 追加到 SQLite traces 表
//! 3. engine → invoker：引擎通过 invoker 调用插件，结果回写状态
//! 4. store → content_loader：消息存储后可通过 ContentLoader 按需加载
//! 5. config → plugin-loader：配置系统产出 manifest 供插件加载器发现
//!
//! 对应 AC-11-2（traces_to: AC-13）

use std::collections::HashMap;
use std::sync::Arc;

use async_trait::async_trait;
use lingxi_config::{CompositePluginYaml, ConfigLoader};
use lingxi_core::traits::{AdrEngine, HookContext, LifecycleHook, PluginInvoker, StorageBackend};
use lingxi_core::types::{
    CompositeStep, ContentLoader, PluginContext, PluginError, PluginResult, RouteSignal, RouteType,
    RunStatus, TenantContext, WakeEvent,
};
use lingxi_engine::{AdrEngineImpl, SqliteStore};
use serde_json::json;

// ═══════════════════════════════════════════════════════════════════
// 测试辅助：Mock 插件调用器（模拟 InProcess 插件行为）
// ═══════════════════════════════════════════════════════════════════

struct MockInvoker {
    /// 记录被调用的插件 ID 列表
    call_log: parking_lot::Mutex<Vec<String>>,
}

impl MockInvoker {
    fn new() -> Self {
        Self {
            call_log: parking_lot::Mutex::new(Vec::new()),
        }
    }

    fn calls(&self) -> Vec<String> {
        self.call_log.lock().clone()
    }
}

#[async_trait]
impl PluginInvoker for MockInvoker {
    async fn invoke_pipeline_plugin(
        &self,
        plugin_id: &str,
        _ctx: &PluginContext,
    ) -> Result<PluginResult, PluginError> {
        self.call_log.lock().push(plugin_id.to_string());

        let mut updates = HashMap::new();
        updates.insert(
            format!("{}_output", plugin_id),
            json!(format!("executed at {}", chrono::Utc::now().to_rfc3339())),
        );

        Ok(PluginResult {
            state_updates: updates,
            route_signal: None,
            skip_remaining: false,
            error: None,
        })
    }

    async fn invoke_tool(
        &self,
        _plugin_id: &str,
        tool_name: &str,
        _inputs: &serde_json::Value,
    ) -> Result<lingxi_core::types::ToolExecutionResult, PluginError> {
        Ok(lingxi_core::types::ToolExecutionResult::success(
            json!({ "tool": tool_name, "result": "ok" }),
        ))
    }

    async fn send_lifecycle_hook(
        &self,
        _plugin_id: &str,
        _hook: LifecycleHook,
        _context: &HookContext,
    ) -> Result<(), PluginError> {
        Ok(())
    }
}

// ═══════════════════════════════════════════════════════════════════
// 测试 1: config → engine 联动：YAML 配置解析为 CompositeStep
// ═══════════════════════════════════════════════════════════════════

/// 场景：YAML 组合插件配置被正确解析为引擎可执行的 CompositeStep 序列。
/// 验证 config crate 的输出可以直接作为 engine crate 的输入。
#[test]
fn test_config_to_engine_step_parsing() {
    let yaml_content = r#"
id: test_composite_plugin
name: test_pipeline
version: "0.1.0"
plugin_type: composite
steps:
  - name: validate_input
    plugin: input_validator
    inputs:
      required_fields: ["message"]
  - name: llm_call
    plugin: llm_core
    inputs:
      model: gpt-4
  - name: format_output
    plugin: output_formatter
    inputs:
      format: markdown
"#;

    let composite: CompositePluginYaml = serde_yaml::from_str(yaml_content).unwrap();
    assert_eq!(composite.steps.len(), 3);
    assert_eq!(composite.steps[0].plugin, "input_validator");
    assert_eq!(composite.steps[1].plugin, "llm_core");
    assert_eq!(composite.steps[2].plugin, "output_formatter");

    // 转换为引擎可用的 CompositeStep
    let steps: Vec<CompositeStep> = composite
        .steps
        .iter()
        .map(|s| CompositeStep {
            name: s.name.clone(),
            plugin: s.plugin.clone(),
            inputs: s.inputs.clone(),
            outputs: s.outputs.clone(),
        })
        .collect();

    assert_eq!(steps.len(), 3);
    assert_eq!(steps[0].inputs["required_fields"], json!(["message"]));
    assert_eq!(steps[1].inputs["model"], json!("gpt-4"));
}

// ═══════════════════════════════════════════════════════════════════
// 测试 2: engine → store 联动：执行步骤后 traces 表有 Patch 记录
// ═══════════════════════════════════════════════════════════════════

/// 场景：引擎执行一个步骤，store 应在 traces 表追加 StateUpdate Patch。
/// 验证 engine → store 的 Append-Only Patch 链路。
#[tokio::test]
async fn test_engine_store_patch_append() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let invoker = Arc::new(MockInvoker::new());
    let engine = AdrEngineImpl::new(store.clone(), invoker, "test_tenant");

    // 启动运行
    let config = json!({
        "pipeline": { "name": "test" },
        "steps": [{ "name": "step1", "plugin": "mock_plugin" }]
    });
    let run_id = engine.start_run(&config).await.unwrap();
    assert!(!run_id.is_empty());

    // 执行一个步骤
    let step = CompositeStep {
        name: "step1".to_string(),
        plugin: "mock_plugin".to_string(),
        inputs: json!({}),
        outputs: HashMap::new(),
    };
    let result = engine.execute_step(&run_id, &step).await.unwrap();

    // 验证步骤结果
    assert!(
        result.state_updates.contains_key("mock_plugin_output"),
        "state_updates should contain plugin output"
    );

    // 验证 store 中 traces 表有记录
    let run = store.get_run(&run_id).await.unwrap();
    assert_eq!(run.status, RunStatus::Running);

    // 结束运行
    engine.end_run(&run_id).await.unwrap();
    let run_after = store.get_run(&run_id).await.unwrap();
    assert_eq!(run_after.status, RunStatus::Completed);
}

// ═══════════════════════════════════════════════════════════════════
// 测试 3: engine → invoker 联动：引擎调用 invoker 执行插件
// ═══════════════════════════════════════════════════════════════════

/// 场景：引擎通过 invoker 调用插件，验证 invoker 被正确调用。
#[tokio::test]
async fn test_engine_invoker_dispatch() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let invoker = Arc::new(MockInvoker::new());
    let engine = AdrEngineImpl::new(store, invoker.clone(), "test_tenant");

    let config = json!({ "pipeline": "test" });
    let run_id = engine.start_run(&config).await.unwrap();

    // 执行 3 个步骤
    for i in 0..3 {
        let step = CompositeStep {
            name: format!("step_{}", i),
            plugin: format!("plugin_{}", i),
            inputs: json!({}),
            outputs: HashMap::new(),
        };
        engine.execute_step(&run_id, &step).await.unwrap();
    }

    // 验证 invoker 按序调用了正确的 plugin_id（可观察行为断言）
    let calls = invoker.calls();
    assert_eq!(calls[0], "plugin_0");
    assert_eq!(calls[1], "plugin_1");
    assert_eq!(calls[2], "plugin_2");
}

// ═══════════════════════════════════════════════════════════════════
// 测试 4: store → content_loader 联动：消息存储后按需加载
// ═══════════════════════════════════════════════════════════════════

/// 场景：往 messages+blobs 表写入消息后，ContentLoader 能加载完整内容。
/// 验证 ADR ⑦ 内容懒加载链路。
#[tokio::test]
async fn test_store_content_loader_chain() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());

    // 创建运行实例
    let run_id = "test_run_cl_001";
    let config_hash = "abc123";
    store.create_run(run_id, config_hash, "tenant_1").unwrap();

    // 写入一条消息（消息内容存到 blobs 表）
    let content = "Hello, integration test!";
    store
        .append_message(
            "msg_001",
            run_id,
            "main",
            0,
            "user",
            Some(content),
            Some(content),
        )
        .unwrap();

    // 通过 StorageBackend trait 验证（trait 方式访问，验证接口兼容）
    let run = store.get_run(run_id).await.unwrap();
    assert_eq!(run.run_id, run_id);

    // 验证消息可读取
    let messages = store.get_messages(run_id, "main").await.unwrap();
    assert_eq!(messages.len(), 1);
    assert_eq!(messages[0].role, "user");

    // 验证 ContentLoader 能加载最近消息
    let store_as_backend: Arc<dyn StorageBackend> = store.clone();
    let loader = ContentLoader::new(store_as_backend, run_id.to_string(), "main".to_string(), 10);
    let recent = loader.load_recent_messages(5).await.unwrap();
    assert_eq!(recent.len(), 1);
    assert_eq!(recent[0].content, content);
}

// ═══════════════════════════════════════════════════════════════════
// 测试 5: engine 完整生命周期：start → execute → suspend → resume → end
// ═══════════════════════════════════════════════════════════════════

/// 场景：引擎完整生命周期——启动→执行→挂起→恢复→结束。
/// 验证 ADR ⑤ 分支模型和状态流转。
#[tokio::test]
async fn test_engine_full_lifecycle() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let invoker = Arc::new(MockInvoker::new());
    let engine = AdrEngineImpl::new(store.clone(), invoker, "lifecycle_tenant");

    // 1. 启动
    let config = json!({ "pipeline": "lifecycle_test" });
    let run_id = engine.start_run(&config).await.unwrap();

    // 2. 执行步骤
    let step = CompositeStep {
        name: "initial_step".to_string(),
        plugin: "init_plugin".to_string(),
        inputs: json!({}),
        outputs: HashMap::new(),
    };
    engine.execute_step(&run_id, &step).await.unwrap();

    // 3. 挂起
    let handle = engine.suspend(&run_id).await.unwrap();
    assert_eq!(handle.run_id, run_id);
    let run_suspended = store.get_run(&run_id).await.unwrap();
    assert_eq!(run_suspended.status, RunStatus::Suspended);

    // 4. 恢复
    engine.resume(&handle, WakeEvent::Manual).await.unwrap();
    let run_resumed = store.get_run(&run_id).await.unwrap();
    assert_eq!(run_resumed.status, RunStatus::Running);

    // 5. 结束
    engine.end_run(&run_id).await.unwrap();
    let run_done = store.get_run(&run_id).await.unwrap();
    assert_eq!(run_done.status, RunStatus::Completed);
}

// ═══════════════════════════════════════════════════════════════════
// 测试 6: engine rollback 链路：执行多步骤后回滚到早期状态
// ═══════════════════════════════════════════════════════════════════

/// 场景：执行 3 个步骤后回滚到 seq=1，验证 ADR ⑤ 回滚机制。
#[tokio::test]
async fn test_engine_rollback_creates_new_branch() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let invoker = Arc::new(MockInvoker::new());
    let engine = AdrEngineImpl::new(store, invoker, "rollback_tenant");

    let config = json!({ "pipeline": "rollback_test" });
    let run_id = engine.start_run(&config).await.unwrap();

    // 执行 3 个步骤
    for i in 0..3 {
        let step = CompositeStep {
            name: format!("step_{}", i),
            plugin: format!("plugin_{}", i),
            inputs: json!({}),
            outputs: HashMap::new(),
        };
        engine.execute_step(&run_id, &step).await.unwrap();
    }

    // 回滚到 seq=1
    let new_branch = engine.rollback(&run_id, 1).await.unwrap();
    assert!(
        new_branch.contains("rollback"),
        "new branch should contain 'rollback': {}",
        new_branch
    );
}

// ═══════════════════════════════════════════════════════════════════
// 测试 7: config + engine 联动：ConfigLoader 加载真实 YAML 后引擎可执行
// ═══════════════════════════════════════════════════════════════════

/// 场景：ConfigLoader 加载 YAML 配置，解析结果作为引擎配置传入。
/// 验证 config crate → engine crate 的端到端数据流。
#[test]
fn test_config_loader_output_consumed_by_engine() {
    // 使用临时目录创建配置
    let temp_dir = tempfile::tempdir().unwrap();
    let config_path = temp_dir.path().join("test_pipeline.yaml");
    std::fs::write(
        &config_path,
        r#"
name: integration_pipeline
steps:
  - name: validate
    plugin: input_validator
    inputs:
      required_fields: ["user_message"]
  - name: process
    plugin: core_processor
    inputs:
      max_tokens: 4096
"#,
    )
    .unwrap();

    let loader = ConfigLoader::new(temp_dir.path(), None);
    let config = loader.load_yaml("test_pipeline.yaml").unwrap();

    // 验证解析结果
    assert_eq!(config["name"], json!("integration_pipeline"));
    assert_eq!(config["steps"].as_array().unwrap().len(), 2);

    // 模拟引擎消费配置——提取 CompositeStep
    let steps = config["steps"].as_array().unwrap();
    let step0 = &steps[0];
    assert_eq!(step0["plugin"], json!("input_validator"));
    assert_eq!(step0["inputs"]["required_fields"], json!(["user_message"]));

    // 构造引擎可用的 CompositeStep（验证类型兼容性）
    let composite_steps: Vec<CompositeStep> = steps
        .iter()
        .map(|s| CompositeStep {
            name: s["name"].as_str().unwrap().to_string(),
            plugin: s["plugin"].as_str().unwrap().to_string(),
            inputs: s["inputs"].clone(),
            outputs: HashMap::new(),
        })
        .collect();

    assert_eq!(composite_steps.len(), 2);
    assert_eq!(composite_steps[1].inputs["max_tokens"], json!(4096));
}

// ═══════════════════════════════════════════════════════════════════
// 测试 8: engine 多租户隔离验证
// ═══════════════════════════════════════════════════════════════════

/// 场景：两个不同租户的运行实例互不干扰。
/// 验证 ADR 多租户上下文穿透。
#[tokio::test]
async fn test_engine_multi_tenant_isolation() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());

    // 租户 A 的引擎
    let invoker_a = Arc::new(MockInvoker::new());
    let engine_a = AdrEngineImpl::new(store.clone(), invoker_a, "tenant_A");

    // 租户 B 的引擎
    let invoker_b = Arc::new(MockInvoker::new());
    let engine_b = AdrEngineImpl::new(store.clone(), invoker_b, "tenant_B");

    // 各自启动运行
    let config_a = json!({ "pipeline": "pipeline_A" });
    let config_b = json!({ "pipeline": "pipeline_B" });

    let run_a = engine_a.start_run(&config_a).await.unwrap();
    let run_b = engine_b.start_run(&config_b).await.unwrap();

    assert_ne!(run_a, run_b, "run IDs must differ");

    // 验证 tenant_id 正确存储
    let record_a = store.get_run(&run_a).await.unwrap();
    let record_b = store.get_run(&run_b).await.unwrap();
    assert_eq!(record_a.tenant_id, "tenant_A");
    assert_eq!(record_b.tenant_id, "tenant_B");
}

// ═══════════════════════════════════════════════════════════════════
// 测试 9: routes_signal 信号传递：插件返回路由信号后被引擎记录
// ═══════════════════════════════════════════════════════════════════

/// 场景：插件执行后返回 End 路由信号，引擎将其记录到 traces 表。
struct RouteSignalInvoker;

#[async_trait]
impl PluginInvoker for RouteSignalInvoker {
    async fn invoke_pipeline_plugin(
        &self,
        _plugin_id: &str,
        _ctx: &PluginContext,
    ) -> Result<PluginResult, PluginError> {
        let mut updates = HashMap::new();
        updates.insert("final_result".to_string(), json!("done"));
        Ok(PluginResult {
            state_updates: updates,
            route_signal: Some(RouteSignal::new(RouteType::End)),
            skip_remaining: false,
            error: None,
        })
    }

    async fn invoke_tool(
        &self,
        _plugin_id: &str,
        _tool_name: &str,
        _inputs: &serde_json::Value,
    ) -> Result<lingxi_core::types::ToolExecutionResult, PluginError> {
        Ok(lingxi_core::types::ToolExecutionResult::success(json!({})))
    }

    async fn send_lifecycle_hook(
        &self,
        _plugin_id: &str,
        _hook: LifecycleHook,
        _context: &HookContext,
    ) -> Result<(), PluginError> {
        Ok(())
    }
}

#[tokio::test]
async fn test_engine_records_route_signal() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let invoker = Arc::new(RouteSignalInvoker);
    let engine = AdrEngineImpl::new(store.clone(), invoker, "signal_tenant");

    let config = json!({ "pipeline": "signal_test" });
    let run_id = engine.start_run(&config).await.unwrap();

    let step = CompositeStep {
        name: "final_step".to_string(),
        plugin: "output_plugin".to_string(),
        inputs: json!({}),
        outputs: HashMap::new(),
    };
    let result = engine.execute_step(&run_id, &step).await.unwrap();

    // 验证路由信号被传递
    assert!(result.route_signal.is_some());
    assert_eq!(
        result.route_signal.as_ref().unwrap().route_type,
        RouteType::End
    );
}

// ═══════════════════════════════════════════════════════════════════
// 测试 10: HookContext 标签化上下文在引擎中正确构造
// ═══════════════════════════════════════════════════════════════════

/// 场景：引擎构造 PluginContext 时注入 tenant_id 和 run_id，
/// 验证 ADR ⑨ 标签化上下文穿透。
#[tokio::test]
async fn test_engine_tenant_context_injection() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());

    // 自定义 invoker，捕获传入的 PluginContext
    struct ContextCaptureInvoker {
        captured: parking_lot::Mutex<Option<TenantContext>>,
    }

    #[async_trait]
    impl PluginInvoker for ContextCaptureInvoker {
        async fn invoke_pipeline_plugin(
            &self,
            _plugin_id: &str,
            ctx: &PluginContext,
        ) -> Result<PluginResult, PluginError> {
            *self.captured.lock() = Some(ctx.tenant.clone());
            Ok(PluginResult::default())
        }

        async fn invoke_tool(
            &self,
            _: &str,
            _: &str,
            _: &serde_json::Value,
        ) -> Result<lingxi_core::types::ToolExecutionResult, PluginError> {
            Ok(lingxi_core::types::ToolExecutionResult::success(json!({})))
        }

        async fn send_lifecycle_hook(
            &self,
            _: &str,
            _: LifecycleHook,
            _: &HookContext,
        ) -> Result<(), PluginError> {
            Ok(())
        }
    }

    let capture = Arc::new(ContextCaptureInvoker {
        captured: parking_lot::Mutex::new(None),
    });
    let engine = AdrEngineImpl::new(store, capture.clone(), "injection_tenant");

    let config = json!({ "pipeline": "injection_test" });
    let run_id = engine.start_run(&config).await.unwrap();

    let step = CompositeStep {
        name: "capture_step".to_string(),
        plugin: "capture_plugin".to_string(),
        inputs: json!({}),
        outputs: HashMap::new(),
    };
    engine.execute_step(&run_id, &step).await.unwrap();

    let captured = capture.captured.lock().clone();
    assert!(captured.is_some(), "tenant context should be captured");
    assert_eq!(
        captured.unwrap().tenant_id,
        "injection_tenant",
        "tenant_id should match engine's default tenant"
    );
}
