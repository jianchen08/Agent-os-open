// @feature: FP-0.2.一 插件协议 | @ci: rust-test
// 由 invoker.rs 的主 #[cfg(test)] 测试块体平移而来（保留私有项访问）。

use super::*;
use agentos_core::traits::{
    LoadedPlugin, McpConfig, McpEndpoint, McpTransport, PluginLifecycle, PluginManifest,
    PluginStatus,
};
use agentos_core::types::TenantContext;
use serde_json::json;
use uuid::Uuid;

/// 串行化 cdylib 加载的 native e2e 测试。
/// 直接 trait 对象的 root module 加载用全局初始化，多线程并发加载不同 cdylib 会竞争，
/// 需串行（生产环境单调用串行，无此问题）。
static NATIVE_E2E_LOCK: parking_lot::Mutex<()> = parking_lot::Mutex::new(());

/// Mock PluginLoader for testing
struct MockLoader {
    manifests: RwLock<HashMap<String, PluginManifest>>,
    loaded: RwLock<HashMap<String, LoadedPlugin>>,
    /// task_11 N7 测试用：plugin_id → 插件目录路径（get_plugin_dir 返回它）。
    plugin_dirs: RwLock<HashMap<String, String>>,
    /// discover 收到的 roots（collect_plugin_roots 行为观察用）。
    captured_roots: RwLock<Vec<Vec<String>>>,
}

impl MockLoader {
    fn new() -> Self {
        Self {
            manifests: RwLock::new(HashMap::new()),
            loaded: RwLock::new(HashMap::new()),
            plugin_dirs: RwLock::new(HashMap::new()),
            captured_roots: RwLock::new(Vec::new()),
        }
    }

    fn add_manifest(&self, manifest: PluginManifest) {
        self.manifests.write().insert(manifest.id.clone(), manifest);
    }
}

#[async_trait]
impl PluginLoader for MockLoader {
    async fn discover(&self, root_paths: &[&str]) -> Result<Vec<PluginManifest>, PluginError> {
        self.captured_roots
            .write()
            .push(root_paths.iter().map(|s| s.to_string()).collect());
        Ok(self.manifests.read().values().cloned().collect())
    }

    fn validate_manifest(&self, _manifest: &PluginManifest) -> Result<(), PluginError> {
        Ok(())
    }

    async fn load(&self, plugin_id: &str) -> Result<LoadedPlugin, PluginError> {
        let manifests = self.manifests.read();
        let manifest = manifests.get(plugin_id).ok_or_else(|| PluginError {
            message: format!("plugin not found: {}", plugin_id),
            code: Some("NOT_FOUND".to_string()),
            source: None,
        })?;

        let loaded = LoadedPlugin {
            manifest: manifest.clone(),
            status: PluginStatus::Active,
            loaded_at: Some(chrono::Utc::now()),
        };

        self.loaded
            .write()
            .insert(plugin_id.to_string(), loaded.clone());

        Ok(loaded)
    }

    async fn unload(&self, plugin_id: &str) -> Result<(), PluginError> {
        self.loaded.write().remove(plugin_id);
        Ok(())
    }

    fn get_plugin_dir(&self, plugin_id: &str) -> Option<String> {
        self.plugin_dirs.read().get(plugin_id).cloned()
    }

    fn get_status(&self, plugin_id: &str) -> PluginStatus {
        self.loaded
            .read()
            .get(plugin_id)
            .map(|p| p.status.clone())
            .unwrap_or(PluginStatus::Discovered)
    }

    fn get_manifest(&self, plugin_id: &str) -> Option<PluginManifest> {
        self.manifests.read().get(plugin_id).cloned()
    }
}

#[allow(dead_code)]
fn make_sidecar_manifest(id: &str, entry: &str) -> PluginManifest {
    PluginManifest {
        force_include_tools: Vec::new(),
        state: None,
        id: id.to_string(),
        name: format!("Test {}", id),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::Tool,
        pipeline_role: None,
        language: "python".to_string(),
        host_type: HostType::Sidecar,
        host_group: None,
        entry: entry.to_string(),
        capabilities: Default::default(),
        requires_services: vec![],
        permissions: Default::default(),
        priority: 100,
        mcp: None,
        lifecycle: None,
        native: None,
        granted_capabilities: vec![],
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        enabled: None,
        activation: None,
        provides: None,
        persistent_fields: vec![],
        export_fields: vec![],
    }
}

/// 构造 light 合宿 sidecar manifest（host_group="light"，合宿路由测试用）。
fn make_light_manifest(id: &str, entry: &str) -> PluginManifest {
    let mut m = make_sidecar_manifest(id, entry);
    m.host_group = Some("light".to_string());
    m
}

fn make_inprocess_manifest(id: &str) -> PluginManifest {
    PluginManifest {
        force_include_tools: Vec::new(),
        state: None,
        id: id.to_string(),
        name: format!("Test {}", id),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::Pipeline,
        pipeline_role: None,
        language: "rust".to_string(),
        host_type: HostType::InProcess,
        host_group: None,
        entry: "test_entry".to_string(),
        capabilities: Default::default(),
        requires_services: vec![],
        permissions: Default::default(),
        priority: 100,
        mcp: None,
        lifecycle: None,
        native: None,
        granted_capabilities: vec![],
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        enabled: None,
        activation: None,
        provides: None,
        persistent_fields: vec![],
        export_fields: vec![],
    }
}

#[test]
fn test_parse_entry_simple() {
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);
    let (cmd, args) = invoker.parse_entry("python3 server.py").unwrap();
    assert_eq!(cmd, "python3");
    assert_eq!(args, vec!["server.py"]);
}

#[test]
fn test_parse_entry_with_args() {
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);
    let (cmd, args) = invoker
        .parse_entry("python3 -m my_plugin --port 8080")
        .unwrap();
    assert_eq!(cmd, "python3");
    assert_eq!(args, vec!["-m", "my_plugin", "--port", "8080"]);
}

#[test]
fn test_parse_entry_empty() {
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);
    assert!(invoker.parse_entry("").is_err());
}

#[tokio::test]
async fn test_invoke_inprocess_without_loader_errors() {
    // InProcess 插件路径已接通 NativePluginLoader：未注入 loader 时应返回
    // NATIVE_LOADER_NOT_CONFIGURED（而非旧的 INPROCESS_DIRECT_CALL 硬错误）。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_inprocess_manifest("rust_plugin"));

    let invoker = PluginInvokerImpl::new(loader);
    let state = json!({});
    let ctx = PluginContext::new(
        &state,
        json!({}),
        TenantContext::new("t1", "s1"),
        Uuid::new_v4(),
        agentos_core::types::ContentLoader::new(
            std::sync::Arc::new(MockStorage),
            "run1".to_string(),
            "main".to_string(),
        ),
    );

    let result = invoker.invoke_pipeline_plugin("rust_plugin", &ctx).await;
    assert!(result.is_err());
    let err = result.unwrap_err();
    assert_eq!(err.code.as_deref(), Some("NATIVE_LOADER_NOT_CONFIGURED"));
}

// ── 端到端打通验证（真实插件产物，非 mock）──────────────────────────
//
// 这两个测试用 plugins/shared 下真实的 native_test（cdylib）和 wasm_hello（.wasm）
// 插件，验证「放进插件目录 + 注入 runtime → 即可调用」的契约。
// 这是 Native/WASM 两种执行模式真正端到端打通的最强证据。
// 若产物未构建，测试 SKIP 而非失败（产物构建属独立步骤）。

/// 仓库根（invoker crate 在 kernel/crates/invoker，项目根在其上三级）。
fn repo_root() -> std::path::PathBuf {
    std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent() // kernel/crates
        .unwrap()
        .parent() // kernel
        .unwrap()
        .parent() // 项目根
        .unwrap()
        .to_path_buf()
}

/// 构造完整装配的 invoker：真实 loader discover plugins/shared + 注入 native runtime。
async fn fully_wired_invoker_for_e2e() -> PluginInvokerImpl {
    let plugins_dir = repo_root().join("plugins/shared");
    let loader = Arc::new(agentos_plugin_loader::PluginLoaderImpl::new(
        plugins_dir.clone(),
        None,
    ));
    loader.discover(&[]).await.unwrap();
    let native_loader = Arc::new(NativePluginLoader::new());
    PluginInvokerImpl::new(loader).set_native_loader(native_loader)
}

fn make_e2e_ctx(state: &serde_json::Value) -> PluginContext<'_> {
    PluginContext::new(
        state,
        json!({}),
        TenantContext::new("t1", "s1"),
        Uuid::new_v4(),
        agentos_core::types::ContentLoader::new(
            std::sync::Arc::new(MockStorage),
            "run1".to_string(),
            "main".to_string(),
        ),
    )
}

#[tokio::test(flavor = "multi_thread")]
// 测试串行化锁需覆盖整个 async 测试体，刻意跨 await 持有，改异步锁会改变串行语义。
#[allow(clippy::await_holding_lock)]
async fn e2e_native_plugins_load_and_execute() {
    // 验证直接 trait 对象改造后 tool_core 原生插件能加载 + 经 HostServices 真正执行工具。
    // 注：NativeHostServices 用 block_in_place（需 multi_thread runtime，生产内核即此配置）。
    //
    // 注意：直接 trait 对象的 RootModule 按 NativePluginModule_Ref 类型全局缓存
    // （root_module_statics 全局单例）。同进程加载多个用同一 RootModule 类型的 cdylib
    // 会互相覆盖。故本测试只验证单个原生插件（tool_core，生产环境的唯一原生插件）。
    let _guard = NATIVE_E2E_LOCK.lock();
    let plugins_dir = repo_root().join("plugins/shared");
    // 按平台定位 tool_core cdylib 产物（与 manifest native.artifact 裸名 +
    // platform_artifact_name 补名逻辑一致：Windows→.dll、Linux→lib{}.so、macOS→lib{}.dylib）。
    // 避免硬编码单一平台后缀导致纯 Linux 环境（仅 .so）静默 SKIP，掩盖真实加载路径。
    let tool_core_artifact = if cfg!(windows) {
        plugins_dir.join("pipeline/core/tool_core/pipeline_tool_core_native.dll")
    } else if cfg!(target_os = "macos") {
        plugins_dir.join("pipeline/core/tool_core/libpipeline_tool_core_native.dylib")
    } else {
        plugins_dir.join("pipeline/core/tool_core/libpipeline_tool_core_native.so")
    };
    if !tool_core_artifact.exists() {
        eprintln!(
            "SKIP: tool_core cdylib not built at {}",
            tool_core_artifact.display()
        );
        return;
    }
    let tool_core_parent = plugins_dir.join("pipeline/core");
    let tool_core_parent_str = tool_core_parent.to_string_lossy().to_string();
    let roots: Vec<&str> = vec![&tool_core_parent_str];
    let loader = Arc::new(agentos_plugin_loader::PluginLoaderImpl::new(
        plugins_dir,
        None,
    ));
    loader.discover(&roots).await.unwrap();
    let native_loader = Arc::new(NativePluginLoader::new());
    let invoker = PluginInvokerImpl::new(loader).set_native_loader(native_loader);

    // 注入 mock router：tool-executor.invoke 模拟 bash_execute 执行成功，
    // 返回 ToolExecutionResult {success:true, data:{output:"agentos-native-ok"}}。
    // 证明原生 tool_core 经 HostServices → router 真正执行工具并拿到结果。
    struct ToolInvokeRouter;
    #[async_trait::async_trait]
    impl CapabilityRouter for ToolInvokeRouter {
        async fn handle(
            &self,
            capability: &str,
            method: &str,
            _params: serde_json::Value,
        ) -> Result<serde_json::Value, agentos_mcp::McpError> {
            match (capability, method) {
                ("tool-executor", "invoke") => {
                    // 回显 tool_name + 返回成功结果（模拟 bash 执行 echo）。
                    Ok(json!({
                        "success": true,
                        "data": {"output": "agentos-native-ok\n", "exit_code": 0},
                        "duration_ms": 1.5,
                    }))
                }
                ("event-bus", "emit") => Ok(json!({"status": "emitted"})),
                _ => Ok(json!({})),
            }
        }
    }
    let router: Arc<dyn CapabilityRouter> = Arc::new(ToolInvokeRouter);
    invoker.set_router(router);

    // tool_core 原生插件（带 raw_tool_calls 触发执行路径）。
    let ctx_tool_state = json!({
        "raw_tool_calls": [
            {"name": "bash_execute", "id": "call_test1", "args": {"command": "echo agentos-native-ok"}}
        ],
        "messages": [],
        "session_id": "test-session",
        "pipeline_id": "test-pipeline",
    });
    let ctx_tool = PluginContext::new(
        &ctx_tool_state,
        json!({}),
        TenantContext::new("t1", "s1"),
        Uuid::new_v4(),
        agentos_core::types::ContentLoader::new(
            std::sync::Arc::new(MockStorage),
            "run1".to_string(),
            "main".to_string(),
        ),
    );
    let result = invoker
        .invoke_pipeline_plugin("pipeline_tool_core", &ctx_tool)
        .await;
    assert!(
        result.is_ok(),
        "tool_core invoke failed: {:?}",
        result.err()
    );
    let pr = result.unwrap();
    // tool_core 必然回写 tool_results + 清空 raw_tool_calls。
    assert!(
        pr.state_updates.contains_key("tool_results"),
        "tool_results missing: {:?}",
        pr.state_updates.keys().collect::<Vec<_>>()
    );
    assert_eq!(pr.state_updates.get("raw_tool_calls"), Some(&json!([])));
    // 关键断言：工具执行成功，结果回写 tool_results（success=true + 输出原文）。
    let tool_results = pr
        .state_updates
        .get("tool_results")
        .and_then(|v| v.as_array())
        .cloned();
    let tr = tool_results.expect("should have tool result array");
    assert_eq!(tr.len(), 1, "should have 1 tool result");
    assert_eq!(tr[0]["success"], true, "tool should succeed: {:?}", tr[0]);
    assert_eq!(
        tr[0]["data"]["output"], "agentos-native-ok\n",
        "tool output should be returned: {:?}",
        tr[0]
    );
    // messages 重建：assistant tool_calls + tool 结果消息（op-based state-update
    // 协议——tool_core native 以 `{"_ops":[{op:"set",msg}]}` 增量下发，非裸数组）。
    let msgs_ops = pr
        .state_updates
        .get("messages")
        .and_then(|v| v.get("_ops"))
        .and_then(|v| v.as_array())
        .cloned();
    let msgs_ops = msgs_ops.expect("messages should be rebuilt (op-based _ops)");
    assert!(!msgs_ops.is_empty(), "新增消息应有 _ops 增量");
    let msgs: Vec<&Value> = msgs_ops.iter().filter_map(|op| op.get("msg")).collect();
    assert!(msgs
        .iter()
        .any(|m| m["role"] == "assistant" && m["tool_calls"].is_array()));
    assert!(
        msgs.iter().any(|m| m["role"] == "tool"
            && m["content"]
                .as_str()
                .map(|s| s.contains("agentos-native-ok"))
                .unwrap_or(false)),
        "tool result message should carry output: {:?}",
        msgs
    );
}

#[tokio::test]
#[ignore = "native_test 与 tool_core 共用 NativePluginModule_Ref 全局缓存，同进程并行会冲突；tool_core 已由 e2e_native_plugins 覆盖。单独跑：cargo test e2e_native_inprocess -- --ignored"]
// 测试串行化锁需覆盖整个 async 测试体，刻意跨 await 持有，改异步锁会改变串行语义。
#[allow(clippy::await_holding_lock)]
async fn e2e_native_inprocess_plugin_executes() {
    // 单独验证 native_test echo 插件（基础 native 直接 trait 对象链路）。
    // 与 e2e_native_plugins 分离：避免同进程两个同 RootModule 类型插件互相覆盖。
    let _guard = NATIVE_E2E_LOCK.lock();
    let dll = repo_root().join("plugins/shared/native_test/native_test_plugin.dll");
    if !dll.exists() {
        eprintln!("SKIP: native cdylib not built at {}", dll.display());
        return;
    }
    let invoker = fully_wired_invoker_for_e2e().await;
    let state = json!({});
    let ctx = make_e2e_ctx(&state);
    let result = invoker.invoke_pipeline_plugin("native_test", &ctx).await;
    assert!(
        result.is_ok(),
        "native plugin invoke failed: {:?}",
        result.err()
    );
    let pr = result.unwrap();
    assert_eq!(
        pr.state_updates.get("processed_by"),
        Some(&json!("test_plugin")),
        "got: {:?}",
        pr.state_updates
    );
}

/// B2：native 工具调用端到端（tool_call 约定字段经 execute C-ABI 直调）。
///
/// 测试形态取舍（诚实记录）：invoke_native_tool 依赖真实 cdylib（loader 是
/// 具体类型 NativePluginLoader，无 mock 注入点），单测无法覆盖完整链路——
/// 归一逻辑由 normalize_native_tool_output 单测覆盖，本测试用真 cdylib
/// （native-sdk-test-plugin 构建产物）验证「PluginCtx.tool_call_json → 插件
/// 工具分支 → ToolExecutionResult 信封」全链路。产物未构建时 SKIP；
/// Windows 下与其他 e2e_native 同因（STATUS_ACCESS_VIOLATION，HEAD 已知），
/// 跑法：cargo test -p agentos-invoker --lib -- --skip e2e_native。
#[tokio::test(flavor = "multi_thread")]
// 测试串行化锁需覆盖整个 async 测试体，刻意跨 await 持有，改异步锁会改变串行语义。
#[allow(clippy::await_holding_lock)]
async fn e2e_native_tool_call_via_execute() {
    let _guard = NATIVE_E2E_LOCK.lock();
    let dll = repo_root().join("plugins/shared/native_test/native_test_plugin.dll");
    if !dll.exists() {
        eprintln!(
            "SKIP: native cdylib not built at {} (build native-sdk-test-plugin and copy)",
            dll.display()
        );
        return;
    }
    let invoker = fully_wired_invoker_for_e2e().await;
    let result = invoker
        .invoke_tool("native_test", "echo_tool", &json!({"hello": "world"}))
        .await;
    assert!(
        result.is_ok(),
        "native tool invoke failed: {:?}",
        result.err()
    );
    let tr = result.unwrap();
    // native-sdk-test-plugin 的工具分支返回 {success:true, data:{tool, echo_args}}
    assert!(tr.success, "tool should succeed: {:?}", tr);
    assert_eq!(tr.data["tool"], "echo_tool");
    assert_eq!(tr.data["echo_args"]["hello"], "world");
}

#[tokio::test]
async fn test_invoke_nonexistent_plugin() {
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);
    let state = json!({});
    let ctx = PluginContext::new(
        &state,
        json!({}),
        TenantContext::new("t1", "s1"),
        Uuid::new_v4(),
        agentos_core::types::ContentLoader::new(
            std::sync::Arc::new(MockStorage),
            "run1".to_string(),
            "main".to_string(),
        ),
    );

    let result = invoker.invoke_pipeline_plugin("nonexistent", &ctx).await;
    assert!(result.is_err());
}

#[tokio::test]
async fn test_crash_callback_invoked() {
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);

    let crashed = Arc::new(std::sync::Mutex::new(None::<String>));
    let crashed_clone = Arc::clone(&crashed);
    invoker.on_crash(Arc::new(move |plugin_id: &str| {
        *crashed_clone.lock().unwrap() = Some(plugin_id.to_string());
    }));

    invoker.notify_crash("test_plugin");

    assert_eq!(*crashed.lock().unwrap(), Some("test_plugin".to_string()));
}

#[tokio::test]
async fn test_lifecycle_hook_composite_skipped() {
    // ADR ⑥: 组合插件不需要生命周期钩子
    let loader = Arc::new(MockLoader::new());
    let manifest = PluginManifest {
        force_include_tools: Vec::new(),
        state: None,
        id: "composite_test".to_string(),
        name: "Composite".to_string(),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::Composite,
        pipeline_role: None,
        language: "yaml".to_string(),
        host_type: HostType::InProcess,
        host_group: None,
        entry: String::new(),
        capabilities: Default::default(),
        requires_services: vec![],
        permissions: Default::default(),
        priority: 100,
        mcp: None,
        lifecycle: None,
        native: None,
        granted_capabilities: vec![],
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        enabled: None,
        activation: None,
        provides: None,
        persistent_fields: vec![],
        export_fields: vec![],
    };
    loader.add_manifest(manifest);

    let invoker = PluginInvokerImpl::new(loader);
    let ctx = HookContext::new();
    let result = invoker
        .send_lifecycle_hook("composite_test", LifecycleHook::OnLoad, &ctx)
        .await;
    assert!(result.is_ok()); // 组合插件直接返回 Ok
}

#[tokio::test]
async fn test_force_unload_nonexistent() {
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);
    // force_unload 对不存在的插件也应该返回 Ok
    let result = invoker.force_unload("nonexistent").await;
    assert!(result.is_ok());
}

// ── B1（M2-reactive 第一刀）：透明恢复包装器单元测试 ──
//
// 测试形态取舍（诚实记录）：完整的「真 sidecar 死亡 → respawn → 重试成功」
// 需要「成功握手后又死亡」的 MCP 进程（echo 插件 + kill 时机控制），单测内
// 需真实 python 进程，太重且 Windows 易脆。这里测**重试逻辑函数层**：
// with_transparent_recovery 以可注入 attempt 闭包暴露，闭包计数模拟
// 「第一次死亡 / 重试成功 / 重试仍失败」三种序列，覆盖恢复决策全部分支；
// 死亡判定（is_dead_sidecar）与错误分类（is_recoverable_sidecar_death）
// 各自独立单测。真进程行为由 e2e_native 家族与集成链路兜底。

#[tokio::test]
async fn test_recovery_retry_once_then_success() {
    // 第一次尝试死亡（PLUGIN_CRASHED）→ force_unload + respawn → 第二次成功：
    // 调用方拿到 Ok（完全透明），且不触发崩溃回调（插件实际可用）。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_sidecar_manifest("recover_ok", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);

    let crashed = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
    let crashed_clone = Arc::clone(&crashed);
    invoker.on_crash(Arc::new(move |plugin_id: &str| {
        crashed_clone.lock().unwrap().push(plugin_id.to_string());
    }));

    let attempts = Arc::new(std::sync::Mutex::new(0u32));
    let attempts_clone = Arc::clone(&attempts);
    let result: Result<serde_json::Value, PluginError> = invoker
        .with_transparent_recovery("recover_ok", || async {
            let n = {
                let mut c = attempts_clone.lock().unwrap();
                *c += 1;
                *c
            };
            if n == 1 {
                Err(PluginError {
                    message: "plugin process died mid-call".to_string(),
                    code: Some("PLUGIN_CRASHED".to_string()),
                    source: Some("plugin-invoker".to_string()),
                })
            } else {
                Ok(serde_json::json!({"recovered": true}))
            }
        })
        .await;

    assert_eq!(*attempts.lock().unwrap(), 2, "死亡后必须恰好重试一次");
    assert_eq!(result.unwrap()["recovered"], true, "重试成功对调用方透明");
    assert!(
        crashed.lock().unwrap().is_empty(),
        "透明恢复成功不应触发崩溃回调"
    );
}

#[tokio::test]
async fn test_recovery_retry_once_only_returns_original_error() {
    // 重试仍失败 → 返回第一次的**原错误**（仅一次重试，防循环），并触发
    // 崩溃回调（恢复失败保留崩溃语义：卸载能力 + last_crash_ts）。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_sidecar_manifest("recover_fail", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);

    let crashed = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
    let crashed_clone = Arc::clone(&crashed);
    invoker.on_crash(Arc::new(move |plugin_id: &str| {
        crashed_clone.lock().unwrap().push(plugin_id.to_string());
    }));

    let attempts = Arc::new(std::sync::Mutex::new(0u32));
    let attempts_clone = Arc::clone(&attempts);
    let result: Result<serde_json::Value, PluginError> = invoker
        .with_transparent_recovery("recover_fail", || async {
            let n = {
                let mut c = attempts_clone.lock().unwrap();
                *c += 1;
                *c
            };
            Err(PluginError {
                message: format!("death #{}", n),
                code: Some("PLUGIN_CRASHED".to_string()),
                source: Some("plugin-invoker".to_string()),
            })
        })
        .await;

    assert_eq!(*attempts.lock().unwrap(), 2, "仅重试一次，不得循环");
    let err = result.unwrap_err();
    assert_eq!(err.code.as_deref(), Some("PLUGIN_CRASHED"));
    assert_eq!(err.message, "death #1", "重试失败返回第一次的原错误");
    assert_eq!(
        crashed.lock().unwrap().as_slice(),
        &["recover_fail".to_string()],
        "恢复失败必须触发一次崩溃回调"
    );
}

#[tokio::test]
async fn test_recovery_non_death_error_no_retry() {
    // 非 death 类失败（MCP_CALL_FAILED 等）不重试——协议/工具错误 respawn 无益。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_sidecar_manifest("no_retry", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);

    let attempts = Arc::new(std::sync::Mutex::new(0u32));
    let attempts_clone = Arc::clone(&attempts);
    let result: Result<serde_json::Value, PluginError> = invoker
        .with_transparent_recovery("no_retry", || async {
            *(attempts_clone.lock().unwrap()) += 1;
            Err(PluginError {
                message: "MCP call failed: protocol error".to_string(),
                code: Some("MCP_CALL_FAILED".to_string()),
                source: Some("plugin-invoker".to_string()),
            })
        })
        .await;

    assert_eq!(*attempts.lock().unwrap(), 1, "非 death 错误不重试");
    assert_eq!(result.unwrap_err().code.as_deref(), Some("MCP_CALL_FAILED"));
}

#[test]
fn test_is_recoverable_sidecar_death_classification() {
    // 死亡分类：PLUGIN_CRASHED 可透明恢复；stdio 运输写失败（MCP_CALL_FAILED
    // 携带 transport error + write/flush/broken pipe 特征）视同死亡可恢复
    // （写失败只可能发生在已死进程句柄上，与 is_dead_sidecar 写失败标记互补）。
    let mk = |code: &str, msg: &str| PluginError {
        message: msg.to_string(),
        code: Some(code.to_string()),
        source: None,
    };
    assert!(PluginInvokerImpl::is_recoverable_sidecar_death(&mk(
        "PLUGIN_CRASHED",
        "x"
    )));
    // 运输写失败 → 可恢复（Windows os error 232 本地化消息同样命中）
    assert!(PluginInvokerImpl::is_recoverable_sidecar_death(&mk(
        "MCP_CALL_FAILED",
        "MCP call failed: tool call failed: isolation_guard.execute: transport error: write newline error: 管道正在被关闭。 (os error 232)"
    )));
    assert!(PluginInvokerImpl::is_recoverable_sidecar_death(&mk(
        "MCP_CALL_FAILED",
        "transport error: flush error: broken pipe"
    )));
    // 非运输失败不恢复：协议错误 / 纯工具错误 / HTTP 网络错误（http post）
    assert!(!PluginInvokerImpl::is_recoverable_sidecar_death(&mk(
        "MCP_CALL_FAILED",
        "MCP call failed: protocol error"
    )));
    assert!(!PluginInvokerImpl::is_recoverable_sidecar_death(&mk(
        "MCP_CALL_FAILED",
        "transport error: http post: connect failed"
    )));
    assert!(!PluginInvokerImpl::is_recoverable_sidecar_death(&mk(
        "MCP_TOOL_CALL_FAILED",
        "transport error: write error: broken pipe"
    )));
    assert!(!PluginInvokerImpl::is_recoverable_sidecar_death(&mk(
        "MCP_CONNECT_FAILED",
        "x"
    )));
    assert!(!PluginInvokerImpl::is_recoverable_sidecar_death(
        &PluginError {
            message: "x".to_string(),
            code: None,
            source: None,
        }
    ));
}

#[test]
fn test_is_dead_sidecar_http_client_never_dead() {
    // HTTP transport 无子进程（pid=None）→ 永不判死（is_alive 恒 false 的坑）。
    // 用未连接的 HTTP 客户端模拟（child=None，与 HTTP 连接后同构——
    // connect 的 HTTP 分支不设置 child）。
    let rt = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    let client = McpClient::new_http(
        "http://127.0.0.1:1/mcp",
        std::collections::HashMap::new(),
        None,
    );
    let dead = rt.block_on(async { PluginInvokerImpl::is_dead_sidecar(&client).await });
    assert!(!dead, "HTTP transport（无子进程）不得判为死亡");
}

/// 构造 StreamableHttp manifest（§3.2 测试辅助）。
fn make_http_manifest(id: &str, url: &str) -> PluginManifest {
    let mut m = make_sidecar_manifest(id, "external");
    m.mcp = Some(McpConfig {
        transport: McpTransport::StreamableHttp,
        endpoint: Some(McpEndpoint {
            url: Some(url.to_string()),
            ..Default::default()
        }),
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: None,
    });
    m
}

#[tokio::test]
async fn test_get_or_create_http_cached_client_not_misjudged_dead() {
    // §3.2：HTTP transport 客户端（pid 恒 None、is_alive 恒
    // false——无子进程）进入缓存后，get_or_create_mcp_client 的 fast path
    // 必须判「存活」：返回同一缓存实例，不触发 notify_crash、不逐出重建。
    // 旧实现裸用 is_alive() 判死，HTTP 插件每次调用都误报 "Plugin process
    // crashed" + 崩溃回调 + 缓存逐出重建（对远程 server 无谓重连）。
    // 红测：修复前本用例失败（缓存被逐出、回调被触发、走重建路径）。
    let loader = Arc::new(MockLoader::new());
    let manifest = make_http_manifest("http_cached", "http://127.0.0.1:9/mcp");
    loader.add_manifest(manifest.clone());

    let invoker = PluginInvokerImpl::new(loader);

    let crashed = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
    let crashed_clone = Arc::clone(&crashed);
    invoker.on_crash(Arc::new(move |plugin_id: &str| {
        crashed_clone.lock().unwrap().push(plugin_id.to_string());
    }));

    // 手工把 HTTP 客户端放进缓存（new_http 未连接态与连接后同构：
    // child 恒 None → pid 恒 None，判定只看 pid 门控）。
    // 缓存键 = 宿主键（外部 MCP 不进合宿组 → 独占键 plugin:{id}）。
    let cached: Arc<tokio::sync::RwLock<McpClient>> = Arc::new(tokio::sync::RwLock::new(
        McpClient::new_http("http://127.0.0.1:9/mcp", HashMap::new(), None),
    ));
    invoker
        .mcp_clients
        .write()
        .insert("plugin:http_cached".to_string(), Arc::clone(&cached));

    let got = invoker
        .get_or_create_mcp_client(&manifest)
        .await
        .expect("HTTP transport 缓存客户端不得被误判死亡");

    // 返回的是同一缓存实例（未重建）
    assert!(
        Arc::ptr_eq(&got, &cached),
        "fast path 必须返回缓存实例，不得逐出重建"
    );
    // 缓存条目未被移除/替换
    let in_cache = invoker
        .mcp_clients
        .read()
        .get("plugin:http_cached")
        .cloned();
    assert!(
        in_cache.map(|c| Arc::ptr_eq(&c, &cached)).unwrap_or(false),
        "缓存条目不得被移除"
    );
    // 未触发崩溃回调
    assert!(
        crashed.lock().unwrap().is_empty(),
        "HTTP transport 误判死会触发 notify_crash"
    );
}

#[tokio::test]
async fn test_get_or_create_dead_stdio_sidecar_still_detected_and_rebuilt() {
    // §3.2 回归护栏：stdio sidecar 真死（pid=Some 且进程
    // 已退出）仍必须判死——notify_crash + 缓存逐出 + 走 respawn 路径。
    // fast path 接入 is_dead_sidecar 门控后该既有语义不得回归。
    //
    // 用「spawn 后立即退出」的进程模拟真死（Windows: cmd /c exit 0；
    // Unix: /bin/sh -c exit 0）。
    #[cfg(windows)]
    let (cmd, args): (&str, Vec<&str>) = ("cmd", vec!["/c", "exit", "0"]);
    #[cfg(unix)]
    let (cmd, args): (&str, Vec<&str>) = ("/bin/sh", vec!["-c", "exit", "0"]);
    let entry = format!("{} {}", cmd, args.join(" "));

    let loader = Arc::new(MockLoader::new());
    let manifest = make_sidecar_manifest("stdio_dead", &entry);
    loader.add_manifest(manifest.clone());

    let invoker = PluginInvokerImpl::new(loader);

    let crashed = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
    let crashed_clone = Arc::clone(&crashed);
    invoker.on_crash(Arc::new(move |plugin_id: &str| {
        crashed_clone.lock().unwrap().push(plugin_id.to_string());
    }));

    // 手工构造「子进程已退出」的 stdio 客户端：connect 真实 spawn 后等它自然
    // 退出。注意：等待期间不得对该客户端调 is_alive/pid（tokio Child 被
    // try_wait 观察到退出后 fuse 成 Done，此后 id()=None）——纯 sleep 等
    // 退出，与生产「首次判定前无人 poll 过 child」的时序一致。
    let mut dying = McpClient::new_stdio(cmd, args.iter().map(|s| s.to_string()).collect());
    dying.connect().await.expect("spawn 速死进程应成功");
    let cached: Arc<tokio::sync::RwLock<McpClient>> = Arc::new(tokio::sync::RwLock::new(dying));
    invoker
        .mcp_clients
        .write()
        .insert("plugin:stdio_dead".to_string(), Arc::clone(&cached));

    // 条件等待子进程真死（llvm-cov 插桩下 spawn+exit 变慢，固定等待会假失败）
    wait_until_dead(&cached).await;

    let result = invoker.get_or_create_mcp_client(&manifest).await;

    // 真死被检测：崩溃回调恰好触发一次（含 plugin_id）
    assert_eq!(
        crashed.lock().unwrap().as_slice(),
        &["stdio_dead".to_string()],
        "stdio 真死必须触发 notify_crash（一次）"
    );
    // 死实例被从缓存逐出
    let in_cache = invoker.mcp_clients.read().get("plugin:stdio_dead").cloned();
    assert!(
        !in_cache.map(|c| Arc::ptr_eq(&c, &cached)).unwrap_or(false),
        "死 sidecar 必须从缓存逐出"
    );
    // 走了 respawn 路径：速死命令完不成 initialize 握手 → Err
    // （而非把死实例当存活返回 Ok）。
    assert!(
        result.is_err(),
        "respawn 后 initialize 必然失败（速死命令），不得返回死实例"
    );
}

/// 条件等待 stdio client 对应子进程真死（try_wait 观察到退出）。
///
/// 固定 sleep 等退出在 llvm-cov 插桩下会假失败（插桩使子进程 spawn+exit
/// 显著变慢）。轮询生产同款判定 [`McpClient::is_dead`]——其 try_wait 在
/// 观察到退出（Child fuse）后仍稳定返回 true，轮询安全。
async fn wait_until_dead(client: &tokio::sync::RwLock<McpClient>) {
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(10);
    while !client.read().await.is_dead().await {
        assert!(
            tokio::time::Instant::now() < deadline,
            "子进程 10s 内未退出（真死前置不成立）"
        );
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }
}

/// kill 后条件等待进程真正退出。
///
/// kill 异步生效（TerminateProcess/SIGKILL 到进程消亡有内核侧窗口），
/// llvm-cov 插桩拉宽该窗口——kill 返回后立即断言 `is_alive()==false` 会
/// 假失败。轮询至退出，超时按断言失败处理。
async fn wait_until_killed(client: &tokio::sync::RwLock<McpClient>, why: &str) {
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(10);
    while client.read().await.is_alive().await {
        assert!(
            tokio::time::Instant::now() < deadline,
            "{why}：kill 后 10s 进程仍未退出"
        );
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }
}

/// 构造「长驻不退出」的 stdio 假 sidecar（§3.3 测试辅助）。
///
/// connect 只 spawn 不做 MCP 握手，任意可执行命令都能当假进程；
/// 选 ping/sleep 是为了它稳定存活 ≥60s（测试窗口内不会自然退出）。
async fn spawn_long_lived_stdio_client() -> McpClient {
    #[cfg(windows)]
    let (cmd, args): (&str, Vec<&str>) = ("cmd", vec!["/c", "ping", "-n", "60", "127.0.0.1"]);
    #[cfg(unix)]
    let (cmd, args): (&str, Vec<&str>) = ("/bin/sh", vec!["-c", "sleep 60"]);
    let mut client = McpClient::new_stdio(cmd, args.iter().map(|s| s.to_string()).collect());
    client
        .connect()
        .await
        .expect("spawn 长驻假 sidecar 进程应成功");
    client
}

#[tokio::test]
async fn test_shutdown_all_drains_and_kills_cached_sidecars() {
    // §3.3a：内核停机口——mcp_clients 必须**全量 drain**
    // 且每个缓存 sidecar 进程被真实 kill（Ctrl-C / exit 75 后零孤儿）。
    // 覆盖两类有区分度的缓存条目：
    // - stdio sidecar（有子进程）：kill 后进程必须死（性质断言，防假实现只清缓存）；
    // - HTTP 客户端（无子进程）：kill 是 no-op，drain 不炸（HTTP transport 混存常态）。
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));

    let live = spawn_long_lived_stdio_client().await;
    assert!(live.is_alive().await, "前置：长驻假 sidecar 必须存活");
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert("stdio_live".to_string(), Arc::clone(&live_arc));
    invoker.mcp_clients.write().insert(
        "http_cached".to_string(),
        Arc::new(tokio::sync::RwLock::new(McpClient::new_http(
            "http://127.0.0.1:9/mcp",
            HashMap::new(),
            None,
        ))),
    );
    assert_eq!(invoker.mcp_clients.read().len(), 2, "前置：缓存 2 条");

    invoker.shutdown_all().await;

    assert!(
        invoker.mcp_clients.read().is_empty(),
        "shutdown_all 后缓存必须清空（全量 drain）"
    );
    assert!(
        !live_arc.read().await.is_alive().await,
        "stdio sidecar 进程必须被 kill（不得留孤儿）"
    );
}

#[tokio::test]
async fn test_shutdown_all_noop_when_cache_empty() {
    // §3.3a 边界：空缓存 no-op 不抛（boot 后从未调用任何插件就停机）。
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    invoker.shutdown_all().await;
    assert!(invoker.mcp_clients.read().is_empty());
}

#[tokio::test]
async fn test_kill_sidecar_if_any_kills_cached_and_noop_when_absent() {
    // §3.3b：disable 窄口——
    // - 有缓存条目：kill 进程 + 移除缓存（reenable 后按调用懒 spawn 重生）；
    // - 无缓存条目（从未 spawn / HTTP 无子进程）：no-op 不抛。
    // 两组输入一次覆盖（有/无是同一行为的有区分度两分支）。
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));

    let live = spawn_long_lived_stdio_client().await;
    assert!(live.is_alive().await, "前置：长驻假 sidecar 必须存活");
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    // 缓存键 = 宿主键（独占：plugin:{id}；victim 无 manifest，走独占兜底键）
    invoker
        .mcp_clients
        .write()
        .insert("plugin:victim".to_string(), Arc::clone(&live_arc));

    // 无缓存：no-op（不 panic、不影响其他条目）
    invoker.kill_sidecar_if_any("never_spawned").await;
    assert!(
        invoker.mcp_clients.read().get("plugin:victim").is_some(),
        "非目标插件缓存不得被误删"
    );

    // 有缓存：kill + 移除
    invoker.kill_sidecar_if_any("victim").await;
    assert!(
        invoker.mcp_clients.read().get("plugin:victim").is_none(),
        "目标插件缓存必须移除"
    );
    assert!(
        !live_arc.read().await.is_alive().await,
        "目标插件 sidecar 进程必须被 kill"
    );

    // 幂等：再次调用同一 id（已无缓存）仍 no-op 不抛
    invoker.kill_sidecar_if_any("victim").await;
}

/// python 可执行名（Windows 为 python，其余为 python3）。
fn python_exe() -> &'static str {
    #[cfg(windows)]
    {
        "python"
    }
    #[cfg(not(windows))]
    {
        "python3"
    }
}

/// python 是否可用（不可用时响亮失败——本组用例依赖真实 python 替身进程）。
fn assert_python_available() {
    let ok = std::process::Command::new(python_exe())
        .arg("--version")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    assert!(ok, "python 不可用——假死评估用例依赖真实 python 替身进程");
}

/// 测试用环境变量守卫：设置并在 Drop 时恢复原值（panic 安全，不污染并行测试）。
struct EnvVarGuard {
    key: &'static str,
    original: Option<String>,
}

impl EnvVarGuard {
    fn set(key: &'static str, value: &str) -> Self {
        let original = std::env::var(key).ok();
        std::env::set_var(key, value);
        Self { key, original }
    }
}

impl Drop for EnvVarGuard {
    fn drop(&mut self) {
        match &self.original {
            Some(v) => std::env::set_var(self.key, v),
            None => std::env::remove_var(self.key),
        }
    }
}

#[tokio::test]
async fn force_unload_completes_bounded_while_read_lock_held() {
    // 锁结构回归：外部调用方持有 client 读锁期间触发 force_unload
    // （respawn/崩溃恢复路径的锁形态），必须限时完成——unload 段（取写锁 →
    // on_unload → kill）限时 2s，读锁被在飞调用持有时放弃 kill（kill_on_drop
    // 兜底）照常完成驱逐与记账。修复前该结构 = 写锁无超时 + 同任务持读锁
    // 等写锁 → 永久悬挂（请求卡死、GC 单循环瘫痪）。
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    let live = spawn_long_lived_stdio_client().await;
    assert!(live.is_alive().await, "前置：长驻假 sidecar 必须存活");
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert("plugin:victim".to_string(), Arc::clone(&live_arc));

    // 模拟 attempt_sidecar_pipeline 失败分支的持锁形态：读锁存活期间 unload。
    let reader = live_arc.read().await;
    let started = tokio::time::Instant::now();
    let outcome = tokio::time::timeout(
        std::time::Duration::from_secs(8),
        invoker.force_unload_impl("victim"),
    )
    .await;
    drop(reader);

    assert!(
        outcome.is_ok(),
        "持读锁期间 force_unload 必须限时完成（自等写锁 = 永久悬挂）"
    );
    let unload_result = outcome.expect("timeout 已排除");
    assert!(
        unload_result.is_ok(),
        "放弃 kill 后卸载状态机必须照常收敛: {:?}",
        unload_result.err()
    );
    let elapsed = started.elapsed();
    assert!(
        elapsed >= std::time::Duration::from_secs(2),
        "写锁被持有必须等满 2s 限时（超时保护生效），实际 {elapsed:?}"
    );
    assert!(
        elapsed < std::time::Duration::from_secs(8),
        "unload 不得在读锁争用下悬挂，实际 {elapsed:?}"
    );
    assert!(
        invoker.mcp_clients.read().get("plugin:victim").is_none(),
        "unload 后缓存条目必须驱逐（下次调用 respawn 新实例）"
    );
    // 显式回收残留进程（读锁已释放，写锁立即可得）：Windows 上 kill_on_drop
    // 只杀 cmd 包装进程不杀 ping 孙进程，弃置会让替身占满其自然生命周期。
    live_arc
        .write()
        .await
        .kill()
        .await
        .expect("残留进程显式回收");
    assert!(
        !live_arc.read().await.is_alive().await,
        "显式回收后进程必须终止"
    );
}

#[tokio::test]
async fn frozen_sidecar_evaluation_then_unlocked_force_unload_kills_process() {
    // 假死评估接缝回归（与 attempt_sidecar_pipeline 失败分支同构的锁序列）：
    // 读锁内完成判定+取证（evaluate_frozen_sidecar 只需 &client），随后释放
    // 读锁再触发 force_unload——限时完成且进程真实被杀、缓存驱逐。
    // 评估只做判定与取证、绝不在调用方读锁存活时执行回收（自等写锁死锁）。
    assert_python_available();

    // 心跳行 #HB 1000（UNIX 毫秒 ≈ 1970）→ heartbeat_age 恒超 30s 阈值，
    // 进程本身存活（sleep 60s）= 「活着但事件循环假死」的确定性替身。
    let script =
        "import sys, time\nsys.stderr.write('#HB 1000\\n'); sys.stderr.flush()\ntime.sleep(60)\n";
    let mut client = McpClient::new_stdio(
        python_exe(),
        vec!["-u".to_string(), "-c".to_string(), script.to_string()],
    );
    client.connect().await.expect("假死替身可连接");
    assert!(client.is_alive().await, "前置：进程存活（非真死）");
    let live_arc = Arc::new(tokio::sync::RwLock::new(client));

    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    invoker
        .mcp_clients
        .write()
        .insert("plugin:frozen".to_string(), Arc::clone(&live_arc));

    // py-spy 注入为必然缺失的二进制：取证降级告警跳过（判定链其余环节照常
    // 走到，且不拖慢测试）。
    let _spy = EnvVarGuard::set("AGENTOS_PY_SPY_BIN", "definitely_not_py_spy_xyz");

    // 等 stderr 读循环记录心跳（connect 返回与记录之间无同步点）。
    let guard = live_arc.read().await;
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(5);
    while guard.heartbeat_age_secs().is_none() {
        assert!(
            tokio::time::Instant::now() < deadline,
            "替身心跳未被记录（#HB 协议契约）"
        );
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }

    let frozen = invoker
        .evaluate_frozen_sidecar(&guard, "frozen", "call failed: synthetic")
        .await;
    assert!(frozen, "心跳停跳超阈值必须判定假死（应触发 force_unload）");
    drop(guard);

    tokio::time::timeout(
        std::time::Duration::from_secs(8),
        invoker.force_unload_impl("frozen"),
    )
    .await
    .expect("释放读锁后 force_unload 必须限时完成")
    .expect("unload 收敛成功");
    wait_until_killed(&live_arc, "假死自愈必须真实 kill 宿主进程").await;
    assert!(
        invoker.mcp_clients.read().get("plugin:frozen").is_none(),
        "unload 后缓存条目必须驱逐（下次调用 respawn 干净实例）"
    );
}

#[tokio::test]
async fn frozen_evaluation_skips_when_heartbeat_absent_or_fresh() {
    // 判定门两分支：从未上报心跳（旧版本 sidecar / HTTP transport）不判假死；
    // 心跳新鲜（事件循环活着）不判假死——只有停跳超阈值才触发自愈。
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));

    // ① 无心跳：未连接 stdio 客户端，last_heartbeat_ms 恒 0 → None。
    let silent = McpClient::new_stdio("cat", vec![]);
    assert!(
        !invoker
            .evaluate_frozen_sidecar(&silent, "no_hb", "call failed")
            .await,
        "无心跳数据不得判定假死（平滑兼容分支）"
    );

    // ② 心跳新鲜：替身启动即上报当前时刻（age ≈ 0s < 30s 阈值）。
    assert_python_available();
    let script = "import sys,time\nsys.stderr.write('#HB %d\\n' % int(time.time()*1000)); sys.stderr.flush()\ntime.sleep(60)\n";
    let mut client = McpClient::new_stdio(
        python_exe(),
        vec!["-u".to_string(), "-c".to_string(), script.to_string()],
    );
    client.connect().await.expect("替身可连接");
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(5);
    while client.heartbeat_age_secs().is_none() {
        assert!(
            tokio::time::Instant::now() < deadline,
            "替身心跳未被记录（#HB 协议契约）"
        );
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }
    assert!(
        !invoker
            .evaluate_frozen_sidecar(&client, "fresh_hb", "call failed")
            .await,
        "心跳新鲜不得判定假死（协议/工具错误走既有语义）"
    );
}

#[tokio::test]
async fn frozen_evaluation_circuit_breaker_and_pidless_dump_degrade_to_no_unload() {
    // 熔断分支：10 分钟窗口内已假死 respawn 满 3 次 → 停手只取证，返回
    // false（不触发 force_unload，防循环重启掩盖根因）。同程覆盖取证降级：
    // 客户端已无子进程（pid None）→ 取证跳过告警，判定链不 panic 不阻断。
    assert_python_available();
    let _spy = EnvVarGuard::set("AGENTOS_PY_SPY_BIN", "definitely_not_py_spy_xyz");

    // 假死替身：记录陈旧心跳后 kill（pid None 语义），heartbeat_age 仍超阈值。
    let script =
        "import sys, time\nsys.stderr.write('#HB 1000\\n'); sys.stderr.flush()\ntime.sleep(60)\n";
    let mut client = McpClient::new_stdio(
        python_exe(),
        vec!["-u".to_string(), "-c".to_string(), script.to_string()],
    );
    client.connect().await.expect("替身可连接");
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(5);
    while client.heartbeat_age_secs().is_none() {
        assert!(
            tokio::time::Instant::now() < deadline,
            "替身心跳未被记录（#HB 协议契约）"
        );
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }
    client.kill().await.expect("kill 释放子进程");
    assert!(
        client.pid().await.is_none(),
        "前置：无子进程（取证降级路径）"
    );

    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    invoker.freeze_respawns.write().insert(
        "frozen_cb".to_string(),
        vec![Instant::now(), Instant::now(), Instant::now()],
    );

    assert!(
        !invoker
            .evaluate_frozen_sidecar(&client, "frozen_cb", "call failed")
            .await,
        "熔断窗口超限必须停手（返回 false，不再触发 force_unload）"
    );
}

#[tokio::test]
async fn idle_gc_pass_completes_bounded_while_read_lock_held() {
    // GC 写锁超时回归：宿主空闲超期但在飞调用方持读锁时，GC pass 必须限时
    // 完成而非悬挂——unload 段 2s 超时后放弃 kill（kill_on_drop 兜底），
    // 驱逐与记账照常收敛。修复前无超时写锁 = GC 单循环在长流宿主上等数小时
    // （全局空闲回收瘫痪）。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_light_manifest("gc_a", "python server.py"));
    loader.add_manifest(make_light_manifest("gc_b", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);

    invoker.resolve_host_key(&make_light_manifest("gc_a", "python server.py"));
    invoker.resolve_host_key(&make_light_manifest("gc_b", "python server.py"));
    let host_key = "group:light:1".to_string();
    let live = spawn_long_lived_stdio_client().await;
    assert!(live.is_alive().await, "前置：长驻假 sidecar 必须存活");
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));
    invoker.touch_last_used(&host_key);
    // 回写旧时间戳：空闲 400s > 默认阈值 300s
    invoker
        .last_used
        .write()
        .insert(host_key.clone(), Instant::now() - Duration::from_secs(400));

    // 在飞调用方持读锁期间跑 GC pass：限时完成 + 超时保护生效 + 驱逐收敛。
    let reader = live_arc.read().await;
    let started = tokio::time::Instant::now();
    tokio::time::timeout(
        std::time::Duration::from_secs(8),
        invoker.run_idle_gc_pass(),
    )
    .await
    .expect("持读锁期间 GC pass 必须限时完成（无超时写锁 = 永久悬挂）");
    drop(reader);
    let elapsed = started.elapsed();
    assert!(
        elapsed >= Duration::from_secs(2),
        "写锁被持有必须等满 2s 限时（超时保护生效），实际 {elapsed:?}"
    );
    assert!(
        invoker.mcp_clients.read().get(&host_key).is_none(),
        "回收即清空：mcp_clients 条目必须移除"
    );
    assert!(
        invoker.last_used.read().get(&host_key).is_none(),
        "回收即清空：last_used 条目必须移除"
    );
    assert!(
        live_arc.read().await.is_alive().await,
        "kill 已因写锁超时放弃：在飞期间进程不被打断（kill_on_drop 在最后一个 Arc 释放时兜底）"
    );
    // 显式回收残留进程（读锁已释放，写锁立即可得）：Windows 上 kill_on_drop
    // 只杀 cmd 包装进程不杀 ping 孙进程，弃置会让替身占满其自然生命周期。
    live_arc
        .write()
        .await
        .kill()
        .await
        .expect("残留进程显式回收");
    assert!(
        !live_arc.read().await.is_alive().await,
        "显式回收后进程必须终止"
    );
}

#[tokio::test]
async fn list_plugin_tools_returns_bounded_error_while_read_lock_held() {
    // G2 回收写锁超时回归：本次新 spawn 的宿主在校验后回收（kill）时，写锁
    // 被并发在飞调用方持有时必须限时返回明确错误，而非无限悬挂。宿主是合法
    // 已初始化实例：缓存条目保留，后续调用照常复用。
    use std::sync::Mutex as StdMutex;

    fn subseq(hay: &[u8], needle: &[u8]) -> Option<usize> {
        hay.windows(needle.len()).position(|w| w == needle)
    }
    fn content_length(headers: &[u8]) -> usize {
        String::from_utf8_lossy(headers)
            .to_ascii_lowercase()
            .lines()
            .find_map(|l| l.strip_prefix("content-length:"))
            .and_then(|v| v.trim().parse().ok())
            .unwrap_or(0)
    }

    // 本地 mock HTTP MCP（环回，出网守卫默认放行）：tools/list 响应延迟
    // 400ms，为并发持锁任务留出确定性抢占窗口；连接内支持 keep-alive 多请求
    // （reqwest 连接池复用：initialize / on_load / tools/list 同连接）。
    async fn spawn_delayed_mock(tools_list_delay: Duration) -> String {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        tokio::spawn(async move {
            loop {
                let Ok((mut sock, _)) = listener.accept().await else {
                    return;
                };
                tokio::spawn(async move {
                    loop {
                        let mut data = Vec::new();
                        let mut tmp = [0u8; 4096];
                        loop {
                            match sock.read(&mut tmp).await {
                                Ok(0) | Err(_) => return,
                                Ok(n) => {
                                    data.extend_from_slice(&tmp[..n]);
                                    if let Some(hdr_end) = subseq(&data, b"\r\n\r\n") {
                                        let cl = content_length(&data[..hdr_end]);
                                        if data.len() >= hdr_end + 4 + cl {
                                            break;
                                        }
                                    }
                                }
                            }
                        }
                        let payload = if data.windows(10).any(|w| w == b"tools/list") {
                            tokio::time::sleep(tools_list_delay).await;
                            r#"{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}"#.to_string()
                        } else {
                            r#"{"jsonrpc":"2.0","id":1,"result":{"ok":true}}"#.to_string()
                        };
                        let out = format!(
                            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
                            payload.len(),
                            payload
                        );
                        if sock.write_all(out.as_bytes()).await.is_err() {
                            return;
                        }
                    }
                });
            }
        });
        url
    }

    let url = spawn_delayed_mock(Duration::from_millis(400)).await;
    let loader = Arc::new(MockLoader::new());
    let manifest = make_http_manifest("g2_contended", &url);
    loader.add_manifest(manifest.clone());
    let invoker = Arc::new(PluginInvokerImpl::new(loader));

    // 并发持锁任务：缓存条目（HTTP 客户端，get_or_create 插入）一出现即持
    // 读锁并保持到测试尾——模拟并发在飞调用。
    let stop = Arc::new(StdMutex::new(false));
    let holder = {
        let invoker = Arc::clone(&invoker);
        let stop = stop.clone();
        tokio::spawn(async move {
            loop {
                let arc = invoker
                    .mcp_clients
                    .read()
                    .get("plugin:g2_contended")
                    .cloned();
                if let Some(arc) = arc {
                    let _guard = arc.read().await;
                    while !*stop.lock().unwrap() {
                        tokio::time::sleep(Duration::from_millis(20)).await;
                    }
                    return true;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
    };

    let started = tokio::time::Instant::now();
    let result = tokio::time::timeout(
        std::time::Duration::from_secs(10),
        invoker.list_plugin_tools("g2_contended"),
    )
    .await
    .expect("持读锁期间 list_plugin_tools 必须限时返回（无超时写锁 = 永久悬挂）");
    let elapsed = started.elapsed();

    let err = result.expect_err("写锁被持有：回收 kill 必须超时报错而非悬挂");
    assert_eq!(
        err.code.as_deref(),
        Some("HOST_RECLAIM_TIMEOUT"),
        "必须是明确的回收超时错误: {err}"
    );
    assert!(
        elapsed >= Duration::from_secs(2),
        "写锁被持有必须等满 2s 限时（超时保护生效），实际 {elapsed:?}"
    );
    assert!(
        invoker
            .mcp_clients
            .read()
            .get("plugin:g2_contended")
            .is_some(),
        "超时放弃回收：宿主是合法已初始化实例，缓存条目保留供后续调用复用"
    );
    *stop.lock().unwrap() = true;
    assert!(holder.await.expect("持锁任务正常退出"));
}

// ── B2：native 工具调用返回归一单元测试 ──

#[test]
fn test_normalize_native_tool_output_envelope_shapes() {
    // 新工具插件的约定返回：{success, data} / {success:false, error} 信封直用。
    let ok = normalize_native_tool_output(&json!({
        "success": true, "data": {"output": "hi"}, "duration_ms": 7
    }));
    assert!(ok.success);
    assert_eq!(ok.data["output"], "hi");
    assert_eq!(ok.duration_ms, Some(7), "信封带的 duration_ms 应保留");

    // 失败信封可缺 data（serde 直解析会报 missing field，归一层手构造）
    let fail = normalize_native_tool_output(&json!({
        "success": false, "error": "boom"
    }));
    assert!(!fail.success);
    assert_eq!(fail.error.as_deref(), Some("boom"));

    // 失败信封缺 error 字段 → 通用文案，不 panic
    let fail_no_msg = normalize_native_tool_output(&json!({"success": false}));
    assert!(!fail_no_msg.success);
    assert!(fail_no_msg.error.is_some());
}

#[test]
fn test_normalize_native_tool_output_legacy_pipeline_shape_wraps_success() {
    // 旧 pipeline 插件（忽略 tool_call）返回 state_updates → 纯业务数据包
    // success 信封（零破坏）。注意 state_updates 可能天然含 error 键——
    // 无 success 字段一律按业务数据处理，不误判 failure。
    let legacy = normalize_native_tool_output(&json!({
        "processed_by": "test_plugin",
        "error": null
    }));
    assert!(
        legacy.success,
        "无 success 字段 = 旧插件业务数据，包 success"
    );
    assert_eq!(legacy.data["processed_by"], "test_plugin");
    assert!(
        legacy.data.get("error").is_some(),
        "业务数据原样保留在 data"
    );
}

#[test]
fn test_normalize_native_tool_output_success_without_data() {
    // 带 success=true 但无 data → data=Null（不报 missing field）。
    let r = normalize_native_tool_output(&json!({"success": true}));
    assert!(r.success);
    assert_eq!(r.data, serde_json::Value::Null);
}

// ── normalize_mcp_tool_result（sidecar 决策树）单元测试 ──

#[test]
fn test_normalize_mcp_result_python_toolresult_shapes() {
    // ②-b (A)：ToolResult.to_dict() 信封 {success, output} → data=output
    let ok =
        normalize_mcp_tool_result(json!({"success": true, "output": {"rows": 3}}), "t1").unwrap();
    assert!(ok.success);
    assert_eq!(ok.data, json!({"rows": 3}));

    // ②-b (B)：解包直返的业务 dict 恰带 success 键 → data=inner
    let ok2 =
        normalize_mcp_tool_result(json!({"success": true, "memory_id": "m-1"}), "t1").unwrap();
    assert!(ok2.success);
    assert_eq!(ok2.data, json!({"success": true, "memory_id": "m-1"}));

    // ②-b success=false → failure(error)
    let fail = normalize_mcp_tool_result(json!({"success": false, "error": "boom"}), "t1").unwrap();
    assert!(!fail.success);
    assert_eq!(fail.error.as_deref(), Some("boom"));
}

#[test]
fn test_normalize_mcp_result_error_and_plain_data_shapes() {
    // ① isError=true 提取产物 {"error": "..."}（无 success）→ failure
    let fail = normalize_mcp_tool_result(json!({"error": "isError"}), "t1").unwrap();
    assert!(!fail.success);
    assert_eq!(fail.error.as_deref(), Some("isError"));

    // ③ 纯业务数据 → success(data=inner)
    let plain = normalize_mcp_tool_result(json!({"result": [1, 2]}), "t1").unwrap();
    assert!(plain.success);
    assert_eq!(plain.data, json!({"result": [1, 2]}));
}

#[test]
fn test_normalize_mcp_result_metadata_passthrough() {
    // ②-a 真 ToolExecutionResult 信封：顶层 metadata 透传（task_evaluation_completed
    // / task_failed 等副作用信号的唯一载体，tool_core 侧效派生消费）。
    let ok = normalize_mcp_tool_result(
        json!({
            "success": true,
            "data": {"overall_passed": true, "task_id": "t1"},
            "metadata": {"action": "auto_complete", "result": "completed"},
        }),
        "task_evaluate",
    )
    .unwrap();
    assert!(ok.success);
    assert_eq!(ok.metadata.as_ref().unwrap()["result"], "completed");

    // ②-b：output 信封顶层 metadata 同样保留
    let ok2 = normalize_mcp_tool_result(
        json!({"success": true, "output": {"rows": 3}, "metadata": {"task_failed": true}}),
        "t1",
    )
    .unwrap();
    assert_eq!(ok2.metadata.as_ref().unwrap()["task_failed"], true);

    // ③ 纯业务数据无 metadata → None（不伪造）
    let plain = normalize_mcp_tool_result(json!({"result": [1, 2]}), "t1").unwrap();
    assert!(plain.metadata.is_none());
}

#[test]
fn test_normalize_mcp_result_non_boolean_success_is_parse_error() {
    // K7：success 键存在但非 bool（字符串/整数状态码等信封漂移）→ PARSE_ERROR，
    // 不得 unwrap_or(true) 把失败包装成成功污染下游 state。
    for bad in [
        json!({"success": "true"}),
        json!({"success": 0}),
        json!({"success": "ok", "output": {}}),
    ] {
        let err = normalize_mcp_tool_result(bad, "drifting_tool").unwrap_err();
        assert_eq!(
            err.code.as_deref(),
            Some("PARSE_ERROR"),
            "非布尔 success 必须 PARSE_ERROR: {err}"
        );
        assert!(
            err.message.contains("non-boolean"),
            "报错应指明信封漂移: {err}"
        );
    }
}

// ── extract_mcp_content 辅助函数单元测试 ──

#[test]
fn test_extract_mcp_content_normal_response() {
    let inner_json = r#"{"state_updates":{"key":"value"}}"#;
    let mcp_result = json!({
        "content": [{"type": "text", "text": inner_json}],
        "isError": false
    });
    let extracted = extract_mcp_content(&mcp_result);
    assert_eq!(extracted["state_updates"]["key"], "value");
}

#[test]
fn test_extract_mcp_content_is_error() {
    let mcp_result = json!({
        "content": [{"type": "text", "text": "something went wrong"}],
        "isError": true
    });
    let extracted = extract_mcp_content(&mcp_result);
    assert_eq!(extracted["error"], "something went wrong");
}

#[test]
fn test_extract_mcp_content_empty_content_array() {
    let mcp_result = json!({
        "content": [],
        "isError": false
    });
    let extracted = extract_mcp_content(&mcp_result);
    // 空数组 → and_then 链返回 None → fallback 到 clone 原对象
    assert_eq!(extracted["content"], json!([]));
}

#[test]
fn test_extract_mcp_content_text_not_json() {
    let mcp_result = json!({
        "content": [{"type": "text", "text": "not_a_json_string"}],
        "isError": false
    });
    let extracted = extract_mcp_content(&mcp_result);
    // text 不是合法 JSON → from_str().ok() 返回 None → fallback 到 clone
    assert_eq!(extracted["content"][0]["text"], "not_a_json_string");
}

#[test]
fn test_extract_mcp_content_missing_content_field() {
    let mcp_result = json!({"isError": false});
    let extracted = extract_mcp_content(&mcp_result);
    // 无 content 字段 → fallback 到 clone 原对象
    assert_eq!(extracted["isError"], false);
}

// ── P6 命名治理（ADR 附录 D③）：invoke_pipeline_plugin 读 invoke_entry ──
// 注：build_injected_config / resolve_config_path 及其测试已迁至 shared.rs。

/// 辅助：构造一个 sidecar pipeline manifest（用于 invoke_entry 缺失测试）。
fn make_pipeline_sidecar_manifest(id: &str, invoke_entry: Option<&str>) -> PluginManifest {
    PluginManifest {
        force_include_tools: Vec::new(),
        state: None,
        id: id.to_string(),
        name: format!("Test {}", id),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::Pipeline,
        pipeline_role: None,
        language: "python".to_string(),
        host_type: HostType::Sidecar,
        host_group: None,
        entry: "python server.py".to_string(),
        capabilities: Default::default(),
        requires_services: vec![],
        permissions: Default::default(),
        priority: 100,
        mcp: None,
        lifecycle: None,
        native: None,
        granted_capabilities: vec![],
        requires_content: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        enabled: None,
        activation: None,
        provides: None,
        invoke_entry: invoke_entry.map(str::to_string),
        persistent_fields: vec![],
        export_fields: vec![],
    }
}

/// P6：sidecar pipeline 插件缺 invoke_entry 时，invoke_pipeline_plugin 返回
/// 明确的 MISSING_INVOKE_ENTRY 错误（不再静默回退字面量 "execute"）。
/// 此为运行期防线；启动期聚合校验（plugin-loader discover）是主门。
#[tokio::test]
async fn test_invoke_pipeline_plugin_missing_invoke_entry_returns_error() {
    let loader = Arc::new(MockLoader::new());
    // 缺 invoke_entry 的 sidecar pipeline 插件
    loader.add_manifest(make_pipeline_sidecar_manifest("bad_pipeline", None));

    let invoker = PluginInvokerImpl::new(loader);
    let state = json!({});
    let ctx = PluginContext::new(
        &state,
        json!({}),
        TenantContext::new("t1", "s1"),
        Uuid::new_v4(),
        agentos_core::types::ContentLoader::new(
            std::sync::Arc::new(MockStorage),
            "run1".to_string(),
            "main".to_string(),
        ),
    );

    let result = invoker.invoke_pipeline_plugin("bad_pipeline", &ctx).await;
    assert!(result.is_err(), "missing invoke_entry must error");
    let err = result.unwrap_err();
    assert_eq!(
        err.code.as_deref(),
        Some("MISSING_INVOKE_ENTRY"),
        "error code must be MISSING_INVOKE_ENTRY, got: {:?}",
        err.code
    );
    assert!(
        err.message.contains("bad_pipeline"),
        "error message must name the offending plugin: {}",
        err.message
    );
}

/// W2b：sidecar 路径 state.reads 切片投喂的调用面覆盖。
///
/// 注入未连接的 HTTP 客户端进缓存（HTTP transport 不判死，fast path 直复用，
/// 无需真实服务端），invoke_pipeline_plugin 走到 arguments_json 构造（含
/// state_feed_payload 切片组装，声明/未声明两分支）后 call_tool_raw 对
/// 不可达端口失败——MCP_CALL_FAILED 证明构造段已执行且不 panic。
#[tokio::test]
async fn test_sidecar_pipeline_state_reads_slice_reaches_arguments_construction() {
    for (plugin_id, with_reads) in [("slice_declared", true), ("slice_undeclared", false)] {
        let loader = Arc::new(MockLoader::new());
        let mut manifest = make_http_manifest(plugin_id, "http://127.0.0.1:9/mcp");
        manifest.plugin_type = PluginType::Pipeline;
        manifest.invoke_entry = Some(format!("{plugin_id}.execute"));
        if with_reads {
            manifest.state = Some(agentos_core::traits::StateDeclaration {
                reads: vec!["messages_tail:1".to_string(), "task.status".to_string()],
                ..Default::default()
            });
        }
        loader.add_manifest(manifest);
        let invoker = PluginInvokerImpl::new(loader);
        // 注入未连接 HTTP 客户端（与连接后同构：child 恒 None → 不判死）。
        invoker.mcp_clients.write().insert(
            format!("plugin:{plugin_id}"),
            Arc::new(tokio::sync::RwLock::new(McpClient::new_http(
                "http://127.0.0.1:9/mcp",
                HashMap::new(),
                None,
            ))),
        );

        let state = json!({
            "messages": [{"role": "user"}, {"role": "assistant"}],
            "task.status": "running",
            "pipeline_id": "p-1",
            "undeclared_big": "should-not-panic-either-way",
        });
        let ctx = PluginContext::new(
            &state,
            json!({}),
            TenantContext::new("t1", "s1"),
            Uuid::new_v4(),
            agentos_core::types::ContentLoader::new(
                std::sync::Arc::new(MockStorage),
                "run1".to_string(),
                "main".to_string(),
            ),
        );

        let result = invoker.invoke_pipeline_plugin(plugin_id, &ctx).await;
        let err = result.expect_err("不可达 HTTP 端点必须失败");
        assert_eq!(
            err.code.as_deref(),
            Some("MCP_CALL_FAILED"),
            "{plugin_id}: 应到达 arguments 构造后在 call_tool_raw 失败，got: {err:?}"
        );
    }
}

// Mock StorageBackend for test context
struct MockStorage;

#[async_trait::async_trait]
impl agentos_core::traits::StorageBackend for MockStorage {
    async fn get_run(
        &self,
        _run_id: &str,
    ) -> Result<agentos_core::types::RunRecord, agentos_core::types::StorageError> {
        Err(agentos_core::types::StorageError::NotFound(
            "mock".to_string(),
        ))
    }
    // 注：旧 trait 方法 get_messages/get_recent_messages/next_sequence 已随
    // StorageBackend 演进移除，mock 同步删除（修复 HEAD 上 lib test 编译失败）。
    async fn get_blob(&self, _blob_id: &str) -> Result<Vec<u8>, agentos_core::types::StorageError> {
        Ok(vec![])
    }
    async fn append_trace(
        &self,
        _entry: agentos_core::types::TraceEntry,
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn update_run_status(
        &self,
        _run_id: &str,
        _status: agentos_core::types::RunStatus,
        _branch: Option<&str>,
        _seq: Option<u32>,
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn get_messages_by_pipeline(
        &self,
        _pipeline_id: &str,
        _opts: agentos_core::traits::MessageQueryOpts,
    ) -> Result<Vec<agentos_core::types::MessageRecord>, agentos_core::types::StorageError> {
        Ok(vec![])
    }
    async fn create_run(
        &self,
        _run_id: &str,
        _config_hash: &str,
        _tenant_id: &str,
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn store_blob(
        &self,
        _data: &[u8],
        _mime_type: &str,
    ) -> Result<String, agentos_core::types::StorageError> {
        Ok("mock_blob".to_string())
    }
    async fn create_session(
        &self,
        _session: &agentos_core::types::SessionRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn get_session(
        &self,
        _thread_id: &str,
    ) -> Result<Option<agentos_core::types::SessionRecord>, agentos_core::types::StorageError> {
        Ok(None)
    }
    async fn list_sessions(
        &self,
        _filter: agentos_core::traits::SessionListFilter,
    ) -> Result<Vec<agentos_core::types::SessionRecord>, agentos_core::types::StorageError> {
        Ok(vec![])
    }
    async fn update_session(
        &self,
        _session: &agentos_core::types::SessionRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn delete_session(
        &self,
        _thread_id: &str,
    ) -> Result<Vec<String>, agentos_core::types::StorageError> {
        Ok(Vec::new())
    }
    async fn link_pipeline_session(
        &self,
        _pipeline_id: &str,
        _thread_id: &str,
        _tenant_id: &str,
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn list_pipeline_ids_by_thread(
        &self,
        _thread_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<String>, agentos_core::types::StorageError> {
        Ok(vec![])
    }
    async fn get_step_traces_by_thread(
        &self,
        _thread_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<agentos_core::types::TraceEntry>, agentos_core::types::StorageError> {
        Ok(vec![])
    }
    // ── users（0.5.0 最小持久化）：MockStorage 不实现，返回空
    async fn create_user(
        &self,
        _user: &agentos_core::types::UserRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn get_user_by_id(
        &self,
        _user_id: &str,
    ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        Ok(None)
    }
    async fn get_user_by_username(
        &self,
        _username: &str,
    ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        Ok(None)
    }
    async fn list_users(
        &self,
    ) -> Result<Vec<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        Ok(Vec::new())
    }
    async fn update_user_password(
        &self,
        _user_id: &str,
        _password_hash: &str,
        _must_change_password: bool,
    ) -> Result<bool, agentos_core::types::StorageError> {
        Err(agentos_core::types::StorageError::NotFound(
            "mock".to_string(),
        ))
    }
    async fn update_last_login(
        &self,
        _user_id: &str,
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn delete_user(&self, _user_id: &str) -> Result<bool, agentos_core::types::StorageError> {
        Ok(false)
    }
}

// ── 阶段 1.1 pull 热加载单测 ────────────────────────────────────────────

#[test]
fn test_compute_plugin_fingerprint_stable_for_unchanged_dir() {
    // 同一目录两次计算指纹应相同（mtime 不变）
    let dir = std::env::temp_dir().join("invoker_fp_test_stable");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("plugin.json"), b"{}").unwrap();
    std::fs::write(dir.join("server.py"), b"print(1)").unwrap();
    let manifest = make_sidecar_manifest("test_fp", "python server.py");
    let fp1 = compute_plugin_fingerprint(&dir, &manifest);
    let fp2 = compute_plugin_fingerprint(&dir, &manifest);
    assert_eq!(fp1, fp2, "未变更的目录指纹应稳定");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn test_compute_plugin_fingerprint_changes_on_file_edit() {
    // 修改 server.py 内容（更新 mtime）后指纹应变化
    let dir = std::env::temp_dir().join("invoker_fp_test_change");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("server.py"), b"print(1)").unwrap();
    let manifest = make_sidecar_manifest("test_fp2", "python server.py");
    let fp1 = compute_plugin_fingerprint(&dir, &manifest);
    // 确保跨过 mtime 秒级精度边界
    std::thread::sleep(std::time::Duration::from_secs_f64(1.1));
    std::fs::write(dir.join("server.py"), b"print(2) # changed").unwrap();
    let fp2 = compute_plugin_fingerprint(&dir, &manifest);
    assert_ne!(fp1, fp2, "文件修改后指纹必须变化，否则热加载不会触发");
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn test_is_plugin_stale_ttl_short_circuits() {
    // TTL 内（1s）重复检测不应过期：首次记录后立即再查应返回 false
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader.clone());
    let manifest = make_sidecar_manifest("stale_ttl", "python server.py");
    // 首次：写入指纹，返回 false
    let first = invoker.is_plugin_stale("stale_ttl", &manifest).await;
    assert!(!first, "首次检测不应判为过期");
    // TTL 内立即再查：应短路返回 false
    let second = invoker.is_plugin_stale("stale_ttl", &manifest).await;
    assert!(!second, "TTL 内应短路返回 false（不算过期）");
}

#[tokio::test]
async fn test_force_unload_via_trait_method() {
    // force_unload 是 trait 方法（trait 默认实现被 invoker 覆盖）。
    // 对未加载的插件调 force_unload 应返回 Ok（幂等，无 sidecar 可 kill）。
    let loader = Arc::new(MockLoader::new());
    let invoker: Arc<dyn PluginInvoker> = Arc::new(PluginInvokerImpl::new(loader.clone()));
    let result = invoker.force_unload("never_loaded_plugin").await;
    assert!(result.is_ok(), "force_unload 未加载插件应返回 Ok");
}

#[test]
fn test_touch_last_used_records_activity() {
    // touch_last_used 应在 last_used 缓存写入当前时刻
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);
    assert!(invoker.last_used.read().is_empty(), "初始 last_used 应为空");
    invoker.touch_last_used("plugin_a");
    invoker.touch_last_used("plugin_b");
    assert_eq!(
        invoker.last_used.read().len(),
        2,
        "touch 两个插件后 last_used 应有 2 条"
    );
    // 再次 touch 同一插件应更新（不新增）
    invoker.touch_last_used("plugin_a");
    assert_eq!(invoker.last_used.read().len(), 2, "重复 touch 不应新增条目");
}

#[tokio::test]
async fn test_discover_new_plugins_returns_via_trait() {
    // discover_new_plugins 是 trait 方法，转发到 loader.discover。
    // MockLoader.discover 返回 manifests 缓存里的全部（默认空）。
    let loader = Arc::new(MockLoader::new());
    let invoker: Arc<dyn PluginInvoker> = Arc::new(PluginInvokerImpl::new(loader));
    let result = invoker.discover_new_plugins().await;
    assert!(result.is_ok(), "discover_new_plugins 应返回 Ok");
    assert_eq!(result.unwrap().len(), 0, "空 MockLoader 应发现 0 个插件");
}

// ── venv 单轨（uv 迁移翻转）：路径选择逻辑单元层 ──

/// 造一个假 .venv：只放空文件占位解释器路径（find_venv_interpreter 只做
/// is_file 探测，不执行——真实解释器由 boot 冒烟验证）。
fn fake_venv(dir: &std::path::Path, windows_layout: bool) -> std::path::PathBuf {
    let interp = if windows_layout {
        dir.join(".venv").join("Scripts").join("python.exe")
    } else {
        dir.join(".venv").join("bin").join("python")
    };
    std::fs::create_dir_all(interp.parent().unwrap()).unwrap();
    std::fs::write(&interp, b"").unwrap();
    // 分流门槛是 pyproject + .venv（uv 迁移契约双标志）——默认同时造假 pyproject。
    std::fs::write(dir.join("pyproject.toml"), b"[project]\n").unwrap();
    interp
}

#[test]
fn test_venv_interpreter_layout_both_platforms() {
    // 纯路径选择逻辑：Windows 布局 .venv/Scripts/python.exe，
    // Unix 布局 .venv/bin/python——两分支必须在任意平台上都真实覆盖。
    let dir = std::path::Path::new("D:/proj/plugins/shared/tools/demo");
    let win = venv_interpreter_layout(dir, true);
    assert_eq!(win, dir.join(".venv").join("Scripts").join("python.exe"));
    let unix = venv_interpreter_layout(dir, false);
    assert_eq!(unix, dir.join(".venv").join("bin").join("python"));
}

#[test]
fn test_find_venv_interpreter_detects_windows_layout() {
    // 假 .venv（Scripts/python.exe 占位文件）→ 探测命中返回绝对路径。
    let tmp = tempfile::tempdir().unwrap();
    let expected = fake_venv(tmp.path(), true);
    assert_eq!(find_venv_interpreter(tmp.path()), Some(expected));
}

#[test]
fn test_find_venv_interpreter_detects_unix_layout() {
    // Unix 布局（bin/python）也须可探测——跨平台分支不因运行平台而漏。
    let tmp = tempfile::tempdir().unwrap();
    let expected = fake_venv(tmp.path(), false);
    assert_eq!(find_venv_interpreter(tmp.path()), Some(expected));
}

#[test]
fn test_find_venv_interpreter_missing_venv_returns_none() {
    // 无 .venv → None（resolve_sidecar_command 层据此 fail-closed，单轨）。
    let tmp = tempfile::tempdir().unwrap();
    assert_eq!(find_venv_interpreter(tmp.path()), None);
    // .venv 目录存在但解释器缺失（半成品）同样 None，不误切。
    std::fs::create_dir_all(tmp.path().join(".venv")).unwrap();
    assert_eq!(find_venv_interpreter(tmp.path()), None);
}

#[test]
fn test_is_plain_python_command() {
    // 只有 PATH 裸 python/python3（含 Windows 可执行扩展）才被 venv 替换；
    // 绝对路径/其他解释器（node 等）不动。
    assert!(is_plain_python_command("python"));
    assert!(is_plain_python_command("python3"));
    assert!(is_plain_python_command("python.exe"));
    assert!(!is_plain_python_command("node"));
    assert!(!is_plain_python_command("D:/py/python.exe"));
    assert!(!is_plain_python_command("/usr/bin/python3"));
}

#[test]
fn test_resolve_sidecar_command_uses_venv_interpreter() {
    // 插件目录有 .venv → command 换成 venv 解释器绝对路径，args 原样保留。
    let tmp = tempfile::tempdir().unwrap();
    let interp = fake_venv(tmp.path(), true);

    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "demo_tool".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    let invoker = PluginInvokerImpl::new(loader);
    let manifest = make_sidecar_manifest("demo_tool", "python server.py");

    let (cmd, args) = invoker.resolve_sidecar_command(&manifest).unwrap();
    assert_eq!(cmd, interp.to_string_lossy().into_owned());
    assert_eq!(args, vec!["server.py"]);
}

#[test]
fn test_resolve_sidecar_command_unix_venv_layout() {
    // Unix 布局 venv 同样命中（find 的跨平台回退探测）。
    let tmp = tempfile::tempdir().unwrap();
    let interp = fake_venv(tmp.path(), false);

    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "demo_tool".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    let invoker = PluginInvokerImpl::new(loader);
    let manifest = make_sidecar_manifest("demo_tool", "python server.py");

    let (cmd, _) = invoker.resolve_sidecar_command(&manifest).unwrap();
    assert_eq!(cmd, interp.to_string_lossy().into_owned());
}

#[test]
fn test_resolve_sidecar_command_venv_without_pyproject_fails_closed() {
    // 回归闸：仅 .venv 无 pyproject
    // → Err（PYPROJECT_MISSING）而非回退裸 python——plain 轨已删，半契约状态
    // 必须早失败并给出修复指引（fail-closed）。
    let tmp = tempfile::tempdir().unwrap();
    let interp = if cfg!(windows) {
        tmp.path().join(".venv").join("Scripts").join("python.exe")
    } else {
        tmp.path().join(".venv").join("bin").join("python")
    };
    std::fs::create_dir_all(interp.parent().unwrap()).unwrap();
    std::fs::write(&interp, b"").unwrap();
    assert!(interp.is_file(), "测试前置：假 venv 解释器已就位");

    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "legacy_venv".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    let invoker = PluginInvokerImpl::new(loader);
    let manifest = make_sidecar_manifest("legacy_venv", "python server.py");

    let err = invoker
        .resolve_sidecar_command(&manifest)
        .expect_err("无 pyproject 的孤立 venv 必须失败（批 D 单轨 fail-closed）");
    assert_eq!(err.code.as_deref(), Some("PYPROJECT_MISSING"));
    assert!(
        err.message.contains("pyproject.toml") && err.message.contains("migrate_plugins_to_uv"),
        "错误信息必须含可读修复指引，实际: {}",
        err.message
    );
}

#[test]
fn test_resolve_sidecar_command_no_venv_fails_closed() {
    // 有 pyproject 无 .venv → Err（VENV_INTERPRETER_MISSING），错误含
    // `uv venv`/`uv sync` 重建指引——不再回退 PATH 裸 python。
    let tmp = tempfile::tempdir().unwrap();
    std::fs::write(tmp.path().join("pyproject.toml"), b"[project]\n").unwrap();

    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "demo_tool".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    let invoker = PluginInvokerImpl::new(loader);
    let manifest = make_sidecar_manifest("demo_tool", "python server.py");

    let err = invoker
        .resolve_sidecar_command(&manifest)
        .expect_err("缺 .venv 解释器必须失败（批 D 单轨 fail-closed）");
    assert_eq!(err.code.as_deref(), Some("VENV_INTERPRETER_MISSING"));
    assert!(
        err.message.contains("uv venv") && err.message.contains("uv sync"),
        "错误信息必须含可读修复指引，实际: {}",
        err.message
    );
    // 半成品 .venv（目录存在但解释器缺失）同样失败——不误切也不兜底。
    std::fs::create_dir_all(tmp.path().join(".venv")).unwrap();
    let err2 = invoker
        .resolve_sidecar_command(&manifest)
        .expect_err("半成品 .venv（无解释器）必须失败");
    assert_eq!(err2.code.as_deref(), Some("VENV_INTERPRETER_MISSING"));
}

#[test]
fn test_resolve_sidecar_command_unknown_dir_fails_closed() {
    // loader 查不到插件目录（get_plugin_dir None）→ Err（
    // 原 keeps_plain 双轨语义已删——查不到目录就无法定位 venv，fail-closed）。
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);
    let manifest = make_sidecar_manifest("ghost", "python server.py");
    let err = invoker
        .resolve_sidecar_command(&manifest)
        .expect_err("未知插件目录的 Python sidecar 必须失败");
    assert_eq!(err.code.as_deref(), Some("PLUGIN_DIR_NOT_FOUND"));
}

#[test]
fn test_resolve_sidecar_command_explicit_interpreter_path_untouched() {
    // entry 首词带路径分隔符的显式解释器是刻意选择（is_plain_python_command
    // 不判）→ 原样返回，不替换也不 fail-closed。
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);
    let manifest = make_sidecar_manifest("ghost", "/usr/bin/python3 server.py");
    let (cmd, args) = invoker.resolve_sidecar_command(&manifest).unwrap();
    assert_eq!(cmd, "/usr/bin/python3");
    assert_eq!(args, vec!["server.py"]);
}

#[test]
fn test_resolve_sidecar_command_non_python_entry_not_replaced() {
    // entry 首词非 python（如 node server.js）→ 即使目录有 .venv 也不替换。
    let tmp = tempfile::tempdir().unwrap();
    fake_venv(tmp.path(), true);
    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "demo_tool".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    let invoker = PluginInvokerImpl::new(loader);
    let manifest = make_sidecar_manifest("demo_tool", "node server.js");

    let (cmd, _) = invoker.resolve_sidecar_command(&manifest).unwrap();
    assert_eq!(cmd, "node");
}

// ── 补充分支覆盖：extract_mcp_content 错误/多元素/非字符串 ──

#[test]
fn test_extract_mcp_content_is_error_without_text_uses_default() {
    // isError=true 但 content[0] 无 text 字段 → 用默认错误文案。
    let mcp_result = json!({
        "content": [{"type": "text"}],
        "isError": true
    });
    let extracted = extract_mcp_content(&mcp_result);
    assert_eq!(extracted["error"], "MCP tool returned isError=true");
}

#[test]
fn test_extract_mcp_content_is_error_without_content_uses_default() {
    // isError=true 且无 content 字段 → 用默认错误文案。
    let mcp_result = json!({"isError": true});
    let extracted = extract_mcp_content(&mcp_result);
    assert_eq!(extracted["error"], "MCP tool returned isError=true");
}

#[test]
fn test_extract_mcp_content_multiple_items_takes_first() {
    // content 多元素时取第一项（Python SDK 只产出单元素数组）。
    let mcp_result = json!({
        "content": [
            {"type": "text", "text": r#"{"first": true}"#},
            {"type": "text", "text": r#"{"second": true}"#}
        ],
        "isError": false
    });
    let extracted = extract_mcp_content(&mcp_result);
    assert_eq!(extracted["first"], true);
    assert!(extracted.get("second").is_none());
}

#[test]
fn test_extract_mcp_content_non_string_text_falls_back() {
    // text 不是字符串（如数字）→ 提取失败 → fallback 返回原对象。
    let mcp_result = json!({
        "content": [{"type": "text", "text": 12345}],
        "isError": false
    });
    let extracted = extract_mcp_content(&mcp_result);
    assert_eq!(extracted["content"][0]["text"], 12345);
}

// ── sidecar spawn 失败降级（无真实插件进程）──

/// 构造带 permissions 声明的 sidecar tool manifest（entry 指向不存在的命令）。
fn make_bad_entry_tool_manifest(id: &str) -> PluginManifest {
    let mut m = make_sidecar_manifest(id, "definitely_missing_command_98765 --flag");
    m.permissions = agentos_core::traits::ManifestPermissions {
        network: agentos_core::traits::NetworkPermission {
            allowed_hosts: vec!["example.com".to_string()],
        },
        filesystem: agentos_core::traits::FilesystemPermission {
            read_paths: vec!["/tmp".to_string()],
            write_paths: vec![],
        },
        env_vars: vec!["HOME".to_string()],
        system_calls: vec!["exec".to_string()],
    };
    m
}

#[tokio::test]
async fn test_invoke_tool_sidecar_spawn_failure_returns_mcp_connect_failed() {
    // 入口命令不存在 → spawn 失败 → 降级为 MCP_CONNECT_FAILED 错误（不 panic、不卡死）。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_bad_entry_tool_manifest("tool_spawn_fail"));

    let invoker = PluginInvokerImpl::new(loader);
    let err = invoker
        .invoke_tool("tool_spawn_fail", "some_tool", &json!({"x": 1}))
        .await
        .unwrap_err();
    assert_eq!(err.code.as_deref(), Some("MCP_CONNECT_FAILED"));
}

#[tokio::test]
async fn test_invoke_pipeline_sidecar_spawn_failure_returns_mcp_connect_failed() {
    // pipeline 类型 sidecar：入口命令不存在 → MCP_CONNECT_FAILED。
    let loader = Arc::new(MockLoader::new());
    let mut m = make_pipeline_sidecar_manifest("pipe_spawn_fail", Some("ctx_build.execute"));
    m.entry = "definitely_missing_command_98765 --flag".to_string();
    loader.add_manifest(m);

    let invoker = PluginInvokerImpl::new(loader);
    let state = json!({});
    let ctx = PluginContext::new(
        &state,
        json!({}),
        TenantContext::new("t1", "s1"),
        Uuid::new_v4(),
        agentos_core::types::ContentLoader::new(
            Arc::new(MockStorage),
            "run1".to_string(),
            "main".to_string(),
        ),
    );
    let err = invoker
        .invoke_pipeline_plugin("pipe_spawn_fail", &ctx)
        .await
        .unwrap_err();
    assert_eq!(err.code.as_deref(), Some("MCP_CONNECT_FAILED"));
}

// ── manifest.mcp 配置错误早暴露（不 spawn）──

#[tokio::test]
async fn test_invoke_streamable_http_missing_endpoint_errors() {
    // transport=StreamableHttp 但无 endpoint → MCP_CONFIG_INVALID。
    let loader = Arc::new(MockLoader::new());
    let mut m = make_sidecar_manifest("http_no_ep", "unused entry");
    m.mcp = Some(agentos_core::traits::McpConfig {
        transport: agentos_core::traits::McpTransport::StreamableHttp,
        endpoint: None,
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: None,
    });
    loader.add_manifest(m);

    let invoker = PluginInvokerImpl::new(loader);
    let err = invoker
        .invoke_tool("http_no_ep", "t", &json!({}))
        .await
        .unwrap_err();
    assert_eq!(err.code.as_deref(), Some("MCP_CONFIG_INVALID"));
}

#[tokio::test]
async fn test_invoke_streamable_http_endpoint_missing_url_errors() {
    // transport=StreamableHttp 且有 endpoint 但缺 url → MCP_CONFIG_INVALID。
    let loader = Arc::new(MockLoader::new());
    let mut m = make_sidecar_manifest("http_no_url", "unused entry");
    m.mcp = Some(agentos_core::traits::McpConfig {
        transport: agentos_core::traits::McpTransport::StreamableHttp,
        endpoint: Some(agentos_core::traits::McpEndpoint {
            url: None,
            ..Default::default()
        }),
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: None,
    });
    loader.add_manifest(m);

    let invoker = PluginInvokerImpl::new(loader);
    let err = invoker
        .invoke_tool("http_no_url", "t", &json!({}))
        .await
        .unwrap_err();
    assert_eq!(err.code.as_deref(), Some("MCP_CONFIG_INVALID"));
}

#[tokio::test]
async fn test_invoke_stdio_external_bad_env_placeholder_errors() {
    // Stdio 外部命令的 env 含无法解析的 ${VAR} 占位 → MCP_CONFIG_INVALID
    // （在 spawn 前暴露，避免启动后 import error 卡死）。
    let loader = Arc::new(MockLoader::new());
    let mut m = make_sidecar_manifest("stdio_bad_env", "unused entry");
    let mut env = std::collections::HashMap::new();
    env.insert(
        "PLUGIN_HOME".to_string(),
        "${DEFINITELY_UNSET_VAR_XYZ}".to_string(),
    );
    m.mcp = Some(agentos_core::traits::McpConfig {
        transport: agentos_core::traits::McpTransport::Stdio,
        endpoint: Some(agentos_core::traits::McpEndpoint {
            command: Some("npx".to_string()),
            args: vec!["-y".to_string()],
            env,
            ..Default::default()
        }),
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: None,
    });
    loader.add_manifest(m);

    let invoker = PluginInvokerImpl::new(loader);
    let err = invoker
        .invoke_tool("stdio_bad_env", "t", &json!({}))
        .await
        .unwrap_err();
    assert_eq!(err.code.as_deref(), Some("MCP_CONFIG_INVALID"));
    assert!(
        err.message.contains("env 解析失败"),
        "错误应点名 env 解析失败: {}",
        err.message
    );
}

// ── native tool 调用错误路径（B2 后语义）──

#[tokio::test]
async fn test_invoke_native_tool_without_loader_errors() {
    // B2：invoke_native_tool 已接通 execute 的 tool_call 约定字段——
    // 未注入 loader 时返回 NATIVE_LOADER_NOT_CONFIGURED（与 pipeline 路径一致），
    // 不再是旧的 NATIVE_TOOL_UNSUPPORTED 硬错误。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_inprocess_manifest("native_tool_plug"));

    let invoker = PluginInvokerImpl::new(loader);
    let err = invoker
        .invoke_tool("native_tool_plug", "some_tool", &json!({}))
        .await
        .unwrap_err();
    assert_eq!(err.code.as_deref(), Some("NATIVE_LOADER_NOT_CONFIGURED"));
}

#[tokio::test]
async fn test_invoke_native_tool_missing_artifact_errors() {
    // 注入了 native loader 但 manifest 缺 native.artifact → MISSING_NATIVE_ARTIFACT
    // （B2 后工具路径与 pipeline 路径同门：resolve artifact 是第一道校验）。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_inprocess_manifest("native_tool_no_artifact"));

    let invoker =
        PluginInvokerImpl::new(loader).set_native_loader(Arc::new(NativePluginLoader::new()));
    let err = invoker
        .invoke_tool("native_tool_no_artifact", "some_tool", &json!({}))
        .await
        .unwrap_err();
    assert_eq!(err.code.as_deref(), Some("MISSING_NATIVE_ARTIFACT"));
}

#[tokio::test]
async fn test_invoke_native_tool_bad_artifact_errors() {
    // artifact 指向不存在的文件 → NATIVE_LOAD_FAILED（真实 NativePluginLoader 路径）。
    let loader = Arc::new(MockLoader::new());
    let mut m = make_inprocess_manifest("native_tool_bad_artifact");
    m.native = Some(agentos_core::traits::NativeArtifact {
        artifact: "definitely_missing_plugin.dll".to_string(),
    });
    loader.add_manifest(m);
    loader.plugin_dirs.write().insert(
        "native_tool_bad_artifact".to_string(),
        std::env::temp_dir().to_string_lossy().to_string(),
    );

    let invoker =
        PluginInvokerImpl::new(loader).set_native_loader(Arc::new(NativePluginLoader::new()));
    let err = invoker
        .invoke_tool("native_tool_bad_artifact", "some_tool", &json!({}))
        .await
        .unwrap_err();
    // resolve_artifact 双查后前置报 NOT_FOUND（比拖到 dlopen 的 LOAD_FAILED 更可读）
    assert_eq!(err.code.as_deref(), Some("NATIVE_ARTIFACT_NOT_FOUND"));
}

// ── idle_timeout_secs_sync 优先级链 ──

#[tokio::test]
async fn test_idle_timeout_secs_sync_manifest_never_unload() {
    // manifest 声明 Some(0) = 永不空闲卸载。
    let loader = Arc::new(MockLoader::new());
    let mut m = make_sidecar_manifest("never_idle", "python server.py");
    m.lifecycle = Some(agentos_core::traits::PluginLifecycle {
        idle_timeout_secs: Some(0),
    });
    loader.add_manifest(m);
    let invoker = PluginInvokerImpl::new(loader);
    assert_eq!(invoker.idle_timeout_secs_sync("never_idle"), 0);
}

#[tokio::test]
async fn test_idle_timeout_secs_sync_manifest_value() {
    let loader = Arc::new(MockLoader::new());
    let mut m = make_sidecar_manifest("custom_idle", "python server.py");
    m.lifecycle = Some(agentos_core::traits::PluginLifecycle {
        idle_timeout_secs: Some(42),
    });
    loader.add_manifest(m);
    let invoker = PluginInvokerImpl::new(loader);
    assert_eq!(invoker.idle_timeout_secs_sync("custom_idle"), 42);
}

#[tokio::test]
async fn test_idle_timeout_secs_sync_default() {
    // 未声明 lifecycle → 内核默认 300s。与 env 突变用例互斥（全局基线）。
    let _serial = ENV_SENSITIVE_LOCK.lock();
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_sidecar_manifest("default_idle", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);
    assert_eq!(
        invoker.idle_timeout_secs_sync("default_idle"),
        agentos_core::traits::default_idle_timeout()
    );
}

// ── PluginScopedRouter：_plugin_id 注入（信任锚点）──

/// 记录收到的 capability 调用（供断言 _plugin_id 注入）。
struct RecordRouter {
    calls: std::sync::Arc<std::sync::Mutex<Vec<(String, String, Value)>>>,
}
#[async_trait]
impl CapabilityRouter for RecordRouter {
    async fn handle(
        &self,
        capability: &str,
        method: &str,
        params: Value,
    ) -> Result<Value, agentos_mcp::McpError> {
        self.calls
            .lock()
            .unwrap()
            .push((capability.to_string(), method.to_string(), params));
        Ok(json!({"ok": true}))
    }
    fn known_namespaces(&self) -> Vec<String> {
        vec!["custom-ns".to_string()]
    }
}

#[tokio::test]
async fn test_plugin_scoped_router_injects_plugin_id_into_object_params() {
    let calls = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let scoped: Arc<dyn CapabilityRouter> = Arc::new(PluginScopedRouter {
        plugin_id: "plugin_a".to_string(),
        inner: Arc::new(RecordRouter {
            calls: calls.clone(),
        }),
    });
    let res = scoped
        .handle("metrics", "record", json!({"name": "calls", "value": 1}))
        .await
        .unwrap();
    assert_eq!(res["ok"], true);
    let got = calls.lock().unwrap().clone();
    assert_eq!(got.len(), 1);
    assert_eq!(got[0].2["_plugin_id"], "plugin_a");
    // 原始参数保留
    assert_eq!(got[0].2["name"], "calls");
}

#[tokio::test]
async fn test_plugin_scoped_router_wraps_non_object_params() {
    // 非对象 params（如字符串）→ 包成 {"_plugin_id", "value": params}。
    let calls = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let scoped: Arc<dyn CapabilityRouter> = Arc::new(PluginScopedRouter {
        plugin_id: "plugin_b".to_string(),
        inner: Arc::new(RecordRouter {
            calls: calls.clone(),
        }),
    });
    let _ = scoped.handle("ping", "pong", json!("hello")).await.unwrap();
    let got = calls.lock().unwrap().clone();
    assert_eq!(got[0].2["_plugin_id"], "plugin_b");
    assert_eq!(got[0].2["value"], "hello");
}

#[tokio::test]
async fn test_plugin_scoped_router_known_namespaces_delegates() {
    // known_namespaces 委托给 inner（sidecar initialize 才能拿到插件自注册 namespace）。
    let calls = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let scoped: Arc<dyn CapabilityRouter> = Arc::new(PluginScopedRouter {
        plugin_id: "plugin_c".to_string(),
        inner: Arc::new(RecordRouter { calls }),
    });
    assert_eq!(scoped.known_namespaces(), vec!["custom-ns".to_string()]);
}

// ── 合宿进程模型（§4.1/4.2/4.5/4.6/4.8）──
//
// 测试形态取舍（诚实记录）：完整"真 host.py 合宿"链路依赖宿主侧任务（host.py
// 未落地），这里测**路由/装箱/指纹/GC 逻辑层**——宿主键路由、装箱落点、槽位
// 复用、指纹并集敏感性与整宿主 respawn 触发、spawn 锁粒度、spawn 参数契约。
// 行为级验证用长驻假进程（spawn_long_lived_stdio_client）+ 手工塞缓存模拟
// 已 spawn 宿主，与既有 §3.2/§3.3 测试同构。

#[tokio::test]
async fn test_host_key_routing_light_vs_solo() {
    // 宿主键路由（§4.2 第 1 条）：light → 组键；缺省/其他值/外部 MCP/InProcess
    // → 独占键 plugin:{id}（四组有区分度输入一次覆盖）。
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);

    // light sidecar → 首次装箱 group:light:1
    let light = make_light_manifest("guard_a", "python server.py");
    assert_eq!(invoker.resolve_host_key(&light), "group:light:1");

    // 缺省 host_group → 独占键
    let solo = make_sidecar_manifest("heavy_tool", "python server.py");
    assert_eq!(invoker.resolve_host_key(&solo), "plugin:heavy_tool");

    // 任意合法组名声明即准入（多组分域，驱逐爆炸半径按组隔离）：
    // 非 "light" 组名（如稳定面 "light_stable"）→ 自己组的组键
    let mut stable = make_sidecar_manifest("stable_face", "python server.py");
    stable.host_group = Some("light_stable".to_string());
    assert_eq!(
        invoker.resolve_host_key(&stable),
        "group:light_stable:1",
        "声明组名即准入，槽位按组名分域"
    );

    // 非法组名（含冒号破坏 host_key 结构）→ 保守独占
    let mut bad_group = make_sidecar_manifest("bad_group", "python server.py");
    bad_group.host_group = Some("a:b".to_string());
    assert_eq!(
        invoker.resolve_host_key(&bad_group),
        "plugin:bad_group",
        "非法组名缺省保守独占"
    );

    // light 声明但外部 MCP（进程归外部所有）→ 独占键
    let mut ext = make_light_manifest("ext_light", "external");
    ext.mcp = Some(McpConfig {
        transport: McpTransport::StreamableHttp,
        endpoint: Some(McpEndpoint {
            url: Some("http://127.0.0.1:9/mcp".to_string()),
            ..Default::default()
        }),
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: None,
    });
    assert_eq!(invoker.resolve_host_key(&ext), "plugin:ext_light");
}

#[tokio::test]
async fn test_host_key_of_light_cohost_shared_vs_solo_distinct() {
    // host_key_of（域事件广播按宿主去重的投递单位，BUG-7）：同宿主 light 成员
    // 返回同一装箱键；独占插件键含自身互异；manifest 缺席回退 plugin_id。
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader.clone());
    loader.add_manifest(make_light_manifest("mem_a", "python server.py"));
    loader.add_manifest(make_light_manifest("mem_b", "python server.py"));
    loader.add_manifest(make_sidecar_manifest("solo_x", "python server.py"));

    let key_a = invoker.host_key_of("mem_a");
    let key_b = invoker.host_key_of("mem_b");
    assert!(
        key_a.starts_with("group:light:"),
        "light 成员键是组键: {key_a}"
    );
    assert_eq!(key_a, key_b, "装箱同槽的 light 成员共享宿主键");
    assert_eq!(
        invoker.host_key_of("solo_x"),
        "plugin:solo_x",
        "独占插件键含自身"
    );
    assert_ne!(key_a, invoker.host_key_of("solo_x"));
    assert_eq!(
        invoker.host_key_of("ghost"),
        "ghost",
        "manifest 缺席按独占语义回退 plugin_id"
    );
}

#[tokio::test]
async fn test_light_packing_fill_overflow_sticky() {
    // 装箱落点（§4.5）：未满宿主优先塞入 → 溢出开新宿主 → 分配粘性。
    // max_members=2 注入（不碰环境变量）。
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);

    let assign = |pid: &str| invoker.assign_light_host_with(pid, "light", 2);
    // a,b 塞满 slot 1；c,d 塞满 slot 2；e 溢出开 slot 3
    assert_eq!(assign("a"), "group:light:1", "首个成员开新宿主 slot 1");
    assert_eq!(
        assign("b"),
        "group:light:1",
        "未满宿主优先塞入（粘性到 slot 1）"
    );
    assert_eq!(assign("c"), "group:light:2", "slot 1 满 → 溢出开新宿主");
    assert_eq!(assign("d"), "group:light:2", "slot 2 未满继续塞");
    assert_eq!(assign("e"), "group:light:3", "全部满 → 再开新宿主");

    // 分配粘性：已分配成员重复查询返回原宿主（宿主存活期间归属不变）
    assert_eq!(assign("a"), "group:light:1", "粘性：a 归属不变");
    assert_eq!(assign("c"), "group:light:2", "粘性：c 归属不变");
    // 成员集反查：slot 1 恰含 {a,b}（排序）
    assert_eq!(
        invoker.host_members("group:light:1"),
        vec!["a".to_string(), "b".to_string()]
    );
}

#[tokio::test]
async fn test_light_packing_slot_reuse_after_reclaim() {
    // 槽位复用（§4.5 第 3 条）：宿主被回收（分配表条目清空）后槽位空出，
    // 后续新插件装箱优先复用该宿主，而不是无限开新组。
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);

    let assign = |pid: &str| invoker.assign_light_host_with(pid, "light", 2);
    assert_eq!(assign("a"), "group:light:1");
    assert_eq!(assign("b"), "group:light:1");
    assert_eq!(assign("c"), "group:light:2");
    assert_eq!(assign("d"), "group:light:2");
    assert_eq!(assign("e"), "group:light:3");

    // idle GC 回收 slot 1（reclaim=true 清分配表成员条目）
    invoker.unload_host("group:light:1", true).await.unwrap();
    assert!(
        invoker.host_members("group:light:1").is_empty(),
        "回收后分配表成员条目必须清空"
    );

    // 新成员 f 落点 = 复用 slot 1（计数归 0 优先），不开 slot 4
    assert_eq!(assign("f"), "group:light:1", "回收槽位优先复用");
    // 被回收的老成员 a 重新分配也回 slot 1（分配条目已清，按未满规则落点）
    assert_eq!(assign("a"), "group:light:1");
}

#[tokio::test]
async fn test_multi_group_packing_isolates_groups() {
    // 多组分域（合宿多组扩展）：不同组名各自独立装箱计数与槽位序列——
    // 易变组（light）溢出开新槽不影响稳定组（light_stable）落点；驱逐按
    // 宿主键连坐天然不跨组（unload_host 只杀本组宿主、只清本组成员条目）。
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);

    let assign_light = |pid: &str| invoker.assign_light_host_with(pid, "light", 2);
    let assign_stable = |pid: &str| invoker.assign_light_host_with(pid, "light_stable", 2);

    // 易变组塞满 slot 1
    assert_eq!(assign_light("a"), "group:light:1");
    assert_eq!(assign_light("b"), "group:light:1");
    // 稳定组首成员落自己组的 slot 1，不接手易变组的计数（独立分域）
    assert_eq!(
        assign_stable("mon"),
        "group:light_stable:1",
        "稳定组槽位序列独立于易变组"
    );
    // 易变组溢出开 slot 2，稳定组 slot 1 仍有空位可继续塞
    assert_eq!(assign_light("c"), "group:light:2");
    assert_eq!(assign_stable("cost"), "group:light_stable:1");

    // 成员集反查按组键各归其位
    assert_eq!(
        invoker.host_members("group:light_stable:1"),
        vec!["cost".to_string(), "mon".to_string()]
    );
    assert_eq!(
        invoker.host_members("group:light:1"),
        vec!["a".to_string(), "b".to_string()]
    );

    // 驱逐隔离：回收易变组 slot 1，稳定组成员条目原样保留
    invoker.unload_host("group:light:1", true).await.unwrap();
    assert_eq!(
        invoker.host_members("group:light_stable:1"),
        vec!["cost".to_string(), "mon".to_string()],
        "易变组驱逐不得波及稳定组成员"
    );
}

#[tokio::test]
async fn test_packing_repacks_when_declared_group_changes() {
    // 搬组（多组扩展的粘性修正）：manifest 改组名（light → light_stable）后，
    // 旧装箱条目跨组过期——继续粘在旧组会让成员继续承受旧组驱逐连坐。
    // 装箱判定必须按当前声明组重装箱（旧宿主成员集随之收缩）。
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);

    assert_eq!(
        invoker.assign_light_host_with("mon", "light", 6),
        "group:light:1",
        "初始落易变组"
    );

    // 声明组变更 → 重装箱进新组（旧组条目被覆盖，成员集不再包含 mon）
    assert_eq!(
        invoker.assign_light_host_with("mon", "light_stable", 6),
        "group:light_stable:1",
        "组名变更后按新组重装箱"
    );
    assert_eq!(
        invoker.host_members("group:light:1"),
        Vec::<String>::new(),
        "旧组成员集收缩"
    );
    assert_eq!(
        invoker.host_members("group:light_stable:1"),
        vec!["mon".to_string()]
    );

    // 同组重复查询保持粘性（不因搬组判定破坏既有粘着语义）
    assert_eq!(
        invoker.assign_light_host_with("mon", "light_stable", 6),
        "group:light_stable:1"
    );
}

#[tokio::test]
async fn test_reload_member_paths_without_live_host() {
    // reload_member 三路判定：独占 → Err（watcher 回退 force_unload）；
    // 合宿未装箱（从未调用）→ Ok(no-op)；已装箱但宿主未 spawn → Ok(no-op)
    // ——无存活进程即无进程内代码，下次 spawn 必用新码，无需驱逐任何东西。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_sidecar_manifest("solo_p", "python server.py"));
    loader.add_manifest(make_light_manifest("fresh_m", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);

    assert!(
        invoker.reload_member("solo_p").await.is_err(),
        "独占插件不支持进程内重载（独占本就无连坐面）"
    );
    assert!(
        invoker.reload_member("fresh_m").await.is_ok(),
        "未装箱成员 = 无宿主 = no-op"
    );
    invoker.assign_light_host_with("fresh_m", "light", 6);
    assert!(
        invoker.reload_member("fresh_m").await.is_ok(),
        "装箱但宿主未 spawn = no-op（下次 spawn 必新码）"
    );
}

#[tokio::test]
async fn test_reload_member_end_to_end_with_real_host() {
    // 端到端（真 host.py 合宿宿主）：装箱 → 真 SDK 宿主进程 spawn 入缓存 →
    // reload_member 发 agentos/reload_member 请求 → 宿主进程内重载成员并
    // 应答 → 指纹记账刷新为当前并集（防 pull 路径误判 stale 整组 respawn）。
    // python/SDK 不可用时跳过（CI 环境防御，与 mcp crate 探针约定同款）。
    let ok = std::process::Command::new(python_exe())
        .arg("-c")
        .arg("import agentos_plugin_sdk, mcp")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    if !ok {
        eprintln!("python 或 agentos_plugin_sdk 不可用，跳过 reload 端到端");
        return;
    }

    // 临时 shared_root：单成员 a_service（含 probe 工具），驱动脚本以
    // shared_root 注入启动真 host.py。
    let tmp = tempfile::tempdir().unwrap();
    let member_dir = tmp.path().join("tools").join("a_service");
    std::fs::create_dir_all(&member_dir).unwrap();
    std::fs::write(member_dir.join("plugin.json"), r#"{"id": "a_service"}"#).unwrap();
    std::fs::write(
        member_dir.join("server.py"),
        concat!(
            "from agentos_plugin_sdk import AgentOSPlugin\n",
            "plugin = AgentOSPlugin(\"a_service\")\n",
            "@plugin.tool(name='probe', schema={'type': 'object'})\n",
            "async def probe() -> dict:\n",
            "    return {'value': 1}\n",
        ),
    )
    .unwrap();
    let host_py = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../plugins/shared/_host/host.py");
    let shared_root = tmp.path().to_string_lossy().replace('\\', "/");
    let driver = format!(
        r#"
import sys
sys.path.insert(0, r"{host_py_dir}")
import host
sys.exit(host.main(["--group", "light", "--slot", "1", "--members", "a_service"],
                   shared_root=__import__("pathlib").Path(r"{shared_root}")))
"#,
        host_py_dir = host_py.parent().unwrap().display(),
        shared_root = shared_root,
    );

    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_light_manifest("a_service", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);
    // 装箱（分配表落 group:light:1）
    invoker.assign_light_host_with("a_service", "light", 6);

    let mut client = McpClient::new_stdio(python_exe().to_string(), vec!["-c".to_string(), driver]);
    client.connect().await.expect("真宿主 spawn 应成功");
    client
        .initialize(&serde_json::json!({}))
        .await
        .expect("握手应成功");
    let host_key = "group:light:1".to_string();
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::new(tokio::sync::RwLock::new(client)));
    // 预置过期指纹（陈旧并集）——reload 成功后必须刷新为当前并集
    invoker
        .fingerprints
        .write()
        .insert(host_key.clone(), (42u64, Instant::now()));

    invoker
        .reload_member("a_service")
        .await
        .expect("reload_member 应成功（宿主进程内重载 + 应答）");

    let (fp, _) = *invoker
        .fingerprints
        .read()
        .get(&host_key)
        .expect("指纹条目应存在");
    assert_eq!(
        fp,
        invoker.host_union_fingerprint(&host_key),
        "reload 后指纹记账必须等于当前成员并集（防 stale 误判整组 respawn）"
    );
    invoker.shutdown_all().await;
}

#[tokio::test]
async fn test_reload_member_failure_branches_keep_fingerprint_stale() {
    // 失败两路（真进程应答）：宿主未注册 reload 请求 → 协议错误；宿主应答
    // 缺 reloaded=true → 判失败。两者都必须 Err（watcher 回退整组驱逐），且
    // **不刷新指纹记账**——失败即让 pull 路径按指纹差整组 respawn，语义安全。
    // python/SDK 不可用时跳过（与端到端用例同款探针）。
    let ok = std::process::Command::new(python_exe())
        .arg("-c")
        .arg("import agentos_plugin_sdk, mcp")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    if !ok {
        eprintln!("python 或 agentos_plugin_sdk 不可用，跳过 reload 失败分支");
        return;
    }

    // ① 裸 McpServer（无 reload 请求注册）→ 方法不存在 = 协议错误。
    let plain_driver = r#"
import asyncio
from agentos_plugin_sdk.server import McpServer
server = McpServer(tools={}, resources={}, lifecycle_handlers={})
asyncio.run(server.run())
"#;
    // ② 注册 reload 请求但应答缺 reloaded=true → 判失败。
    let wrong_payload_driver = r#"
import asyncio
from mcp import types
from agentos_plugin_sdk.server import McpServer

async def bad_reload(ctx, params):
    return {"reloaded": False}

server = McpServer(
    tools={}, resources={}, lifecycle_handlers={},
    request_handlers={"agentos/reload_member": (types.RequestParams, bad_reload)},
)
asyncio.run(server.run())
"#;
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);

    for (idx, driver) in [plain_driver, wrong_payload_driver].iter().enumerate() {
        let plugin_id = format!("fail_case_{idx}");
        let host_key = invoker.assign_light_host_with(&plugin_id, "light", 6);
        let mut client = McpClient::new_stdio(
            python_exe().to_string(),
            vec!["-c".to_string(), driver.to_string()],
        );
        client.connect().await.expect("假宿主 spawn 应成功");
        client
            .initialize(&serde_json::json!({}))
            .await
            .expect("握手应成功");
        invoker
            .mcp_clients
            .write()
            .insert(host_key.clone(), Arc::new(tokio::sync::RwLock::new(client)));
        // 预置陈旧指纹：失败路径不得刷新（保持 stale → 回退路径语义正确）。
        invoker
            .fingerprints
            .write()
            .insert(host_key.clone(), (42u64, Instant::now()));

        let err = invoker
            .reload_member(&plugin_id)
            .await
            .expect_err("失败分支必须 Err（触发 watcher 回退 force_unload）");
        assert!(!err.message.is_empty());
        let (fp, _) = *invoker
            .fingerprints
            .read()
            .get(&host_key)
            .expect("指纹条目应存在");
        assert_eq!(fp, 42u64, "失败路径不得刷新指纹（{}）", plugin_id);
    }
    invoker.shutdown_all().await;
}

#[tokio::test]
async fn test_reload_member_response_implies_member_on_load_completed() {
    // BUG-19 契约钉：reload_member 应答成功 ⇒ 成员 execute 面已见 on_load 副作用。
    // 机制：宿主换入（swap_member）只重放 initialize 注入，on_load 重放由
    // reload 请求窗口内的定向派发承担（SDK CohostServer._dispatch_member_load）。
    // 缺失时成员静默半死——换入后 caller 接线（on_load 副作用）永远不落地，
    // 后续每次 execute 都空等（实况：tool_schema 换入后工具面恒空，当日 6 次
    // 换入全部失接线直至整组重建）。本用例以真 host.py 端到端钉住：
    // 换入后调用成员工具，必须读到 on_load 写下的状态。
    let ok = std::process::Command::new(python_exe())
        .arg("-c")
        .arg("import agentos_plugin_sdk, mcp")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    if !ok {
        eprintln!("python 或 agentos_plugin_sdk 不可用，跳过 reload on_load 契约端到端");
        return;
    }

    // 成员模块：模块级计数器 + on_load 自增 + probe 工具读取。reload 后模块
    // 被 host 侧 loader 重新 exec（计数器归零），on_load 定向派发把它拨到 1
    // ——probe 返回 1 当且仅当「换入后 on_load 已重跑」。
    let tmp = tempfile::tempdir().unwrap();
    let member_dir = tmp.path().join("tools").join("a_service");
    std::fs::create_dir_all(&member_dir).unwrap();
    std::fs::write(member_dir.join("plugin.json"), r#"{"id": "a_service"}"#).unwrap();
    std::fs::write(
        member_dir.join("server.py"),
        concat!(
            "from agentos_plugin_sdk import AgentOSPlugin\n",
            "plugin = AgentOSPlugin(\"a_service\")\n",
            "_state = {\"on_load_runs\": 0}\n",
            "@plugin.on_load\n",
            "async def _on_load(params):\n",
            "    _state[\"on_load_runs\"] += 1\n",
            "@plugin.tool(name='probe', schema={'type': 'object'})\n",
            "async def probe() -> dict:\n",
            "    return {'on_load_runs': _state[\"on_load_runs\"]}\n",
        ),
    )
    .unwrap();
    let host_py = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../plugins/shared/_host/host.py");
    let shared_root = tmp.path().to_string_lossy().replace('\\', "/");
    let driver = format!(
        r#"
import sys
sys.path.insert(0, r"{host_py_dir}")
import host
sys.exit(host.main(["--group", "light", "--slot", "1", "--members", "a_service"],
                   shared_root=__import__("pathlib").Path(r"{shared_root}")))
"#,
        host_py_dir = host_py.parent().unwrap().display(),
        shared_root = shared_root,
    );

    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_light_manifest("a_service", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);
    invoker.assign_light_host_with("a_service", "light", 6);

    let mut client = McpClient::new_stdio(python_exe().to_string(), vec!["-c".to_string(), driver]);
    client.connect().await.expect("真宿主 spawn 应成功");
    client
        .initialize(&serde_json::json!({}))
        .await
        .expect("握手应成功");
    let host_key = "group:light:1".to_string();
    let client_arc = Arc::new(tokio::sync::RwLock::new(client));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&client_arc));
    invoker
        .fingerprints
        .write()
        .insert(host_key.clone(), (42u64, Instant::now()));

    invoker
        .reload_member("a_service")
        .await
        .expect("reload_member 应成功（换入 + on_load 定向派发 + 应答）");

    // 换入后的下一次 execute：必须已见 on_load 副作用（换入即接线完成）。
    let result = client_arc
        .read()
        .await
        .call_tool("a_service.probe", &serde_json::json!({}))
        .await
        .expect("换入后成员工具应可达");
    let text = result["content"][0]["text"]
        .as_str()
        .expect("工具结果应含 text content");
    let payload: serde_json::Value =
        serde_json::from_str(text).expect("工具结果 text 应为 JSON 对象");
    assert_eq!(
        payload["on_load_runs"], 1,
        "reload_member 应答成功 ⇒ 换入实例的 on_load 必须已重跑（execute 前接线完成，BUG-19）"
    );
    invoker.shutdown_all().await;
}

#[tokio::test]
async fn test_reload_member_budget_covers_slow_member_initialization() {
    // BUG-19 预算语义：reload 请求窗口自 SDK 起覆盖「模块重 exec + on_load
    // 重放」（宿主在请求处理内定向派发 on_load 后才应答）——慢而未死的成员
    // 初始化不得被掐断：超时即回退 force_unload 整组驱逐，恰是搅动环境的
    // 放大器（一个成员慢初始化 → 全组陪葬 9s 冷启动）。预算须明显大于
    // 合理的慢初始化上界；真挂死宿主仍由本超时 fail-closed 兜底（Err →
    // watcher 回退整组驱逐）。
    let ok = std::process::Command::new(python_exe())
        .arg("-c")
        .arg("import agentos_plugin_sdk, mcp")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    if !ok {
        eprintln!("python 或 agentos_plugin_sdk 不可用，跳过 reload 预算用例");
        return;
    }
    // 假宿主：reload 应答前睡 16s（模拟慢盘/慢解释器下的重 exec + on_load
    // 重放）。16s > 旧预算 15s（红：被掐断回退驱逐）、< 新预算 30s（绿：等足）。
    let slow_driver = r#"
import asyncio
from mcp import types
from agentos_plugin_sdk.server import McpServer

async def slow_reload(ctx, params):
    await asyncio.sleep(16)
    return {"reloaded": True}

server = McpServer(
    tools={}, resources={}, lifecycle_handlers={},
    request_handlers={"agentos/reload_member": (types.RequestParams, slow_reload)},
)
asyncio.run(server.run())
"#;
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);
    let plugin_id = "slow_member";
    let host_key = invoker.assign_light_host_with(plugin_id, "light", 6);
    let mut client = McpClient::new_stdio(
        python_exe().to_string(),
        vec!["-c".to_string(), slow_driver.to_string()],
    );
    client.connect().await.expect("假宿主 spawn 应成功");
    client
        .initialize(&serde_json::json!({}))
        .await
        .expect("握手应成功");
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::new(tokio::sync::RwLock::new(client)));
    invoker
        .fingerprints
        .write()
        .insert(host_key.clone(), (42u64, Instant::now()));

    invoker
        .reload_member(plugin_id)
        .await
        .expect("慢而未死的成员初始化必须等足预算完成，而非超时回退整组驱逐（BUG-19）");
    let (fp, _) = *invoker
        .fingerprints
        .read()
        .get(&host_key)
        .expect("指纹条目应存在");
    assert_eq!(
        fp,
        invoker.host_union_fingerprint(&host_key),
        "慢 reload 完成后指纹记账必须刷新（与快路径同语义）"
    );
    invoker.shutdown_all().await;
}

#[tokio::test]
async fn test_idle_gc_reclaims_whole_host_and_frees_slots() {
    // idle GC 整组回收（§4.8）：宿主空闲（键条目超阈值）→ kill 宿主进程 +
    // 清缓存/指纹/last_used + 分配表成员条目全部释放（槽位复用）。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_light_manifest("guard_a", "python server.py"));
    loader.add_manifest(make_light_manifest("guard_b", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);

    // 装箱 a,b → group:light:1；模拟已 spawn 宿主（长驻假进程入缓存）
    invoker.resolve_host_key(&make_light_manifest("guard_a", "python server.py"));
    invoker.resolve_host_key(&make_light_manifest("guard_b", "python server.py"));
    let host_key = "group:light:1".to_string();
    let live = spawn_long_lived_stdio_client().await;
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));
    invoker.touch_last_used(&host_key);
    invoker
        .fingerprints
        .write()
        .insert(host_key.clone(), (42u64, Instant::now()));

    // 回写旧时间戳：空闲 400s > 默认阈值 300s
    invoker
        .last_used
        .write()
        .insert(host_key.clone(), Instant::now() - Duration::from_secs(400));

    invoker.run_idle_gc_pass().await;

    wait_until_killed(&live_arc, "整组空闲超时必须 kill 宿主进程").await;
    assert!(
        invoker.mcp_clients.read().get(&host_key).is_none(),
        "回收即清空：mcp_clients 条目必须移除"
    );
    assert!(
        invoker.last_used.read().get(&host_key).is_none(),
        "回收即清空：last_used 条目必须移除"
    );
    assert!(
        invoker.fingerprints.read().get(&host_key).is_none(),
        "回收即清空：指纹条目必须移除"
    );
    assert!(
        invoker.light_packing.read().assignments.is_empty(),
        "回收即清空：分配表内该宿主全部成员条目必须释放"
    );
}

#[tokio::test]
async fn test_force_unload_after_idle_reclaim_spares_recycled_slot_new_host() {
    // B15-R1 回归锚（watcher 代码指纹 evict × idle 回收交互）：宿主被 idle GC
    // 回收（分配表清空、槽位复用）后，**被回收老成员**的代码指纹 evict
    // （force_unload）不得误杀复用该槽位的新宿主——驱逐按分配表定位宿主，
    // 无分配条目 = 无宿主可杀；正例对照：evict 现任成员杀其宿主且不回收装箱。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_light_manifest("old_m", "python server.py"));
    loader.add_manifest(make_light_manifest("new_n", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);

    // ① 老成员 old_m 装箱 group:light:1，宿主已 spawn（假进程入缓存）+ 记账
    invoker.resolve_host_key(&make_light_manifest("old_m", "python server.py"));
    let host_key = "group:light:1".to_string();
    let host_m = Arc::new(tokio::sync::RwLock::new(
        spawn_long_lived_stdio_client().await,
    ));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&host_m));
    invoker.touch_last_used(&host_key);

    // ② idle GC 回收：进程 kill + 分配表清空（槽位可复用）
    invoker
        .last_used
        .write()
        .insert(host_key.clone(), Instant::now() - Duration::from_secs(400));
    invoker.run_idle_gc_pass().await;
    wait_until_killed(&host_m, "前置：空闲宿主已被 GC 回收").await;
    assert!(invoker.light_packing.read().assignments.is_empty());

    // ③ 新成员 new_n 装箱进复用的 group:light:1 并 spawn 新宿主
    let assigned = invoker.resolve_host_key(&make_light_manifest("new_n", "python server.py"));
    assert_eq!(assigned, host_key, "前置：新成员复用被回收槽位");
    let host_n = Arc::new(tokio::sync::RwLock::new(
        spawn_long_lived_stdio_client().await,
    ));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&host_n));
    invoker.touch_last_used(&host_key);

    // ④ evict 被回收老成员 old_m：无分配条目 → 无宿主可杀 → 新宿主安然无恙
    invoker.force_unload_impl("old_m").await.unwrap();
    assert!(
        host_n.read().await.is_alive().await,
        "evict 被回收成员不得误杀复用槽位的新宿主"
    );
    assert!(
        invoker.mcp_clients.read().contains_key(&host_key),
        "新宿主缓存条目保持存活"
    );

    // ⑤ 正例对照：evict 现任成员 new_n → 其宿主被杀，装箱保留（respawn 按表重建）
    invoker.force_unload_impl("new_n").await.unwrap();
    wait_until_killed(&host_n, "evict 现任成员必须 kill 其宿主进程").await;
    assert!(!invoker.mcp_clients.read().contains_key(&host_key));
    assert!(
        invoker
            .light_packing
            .read()
            .assignments
            .contains_key("new_n"),
        "evict（force_unload）不回收装箱分配——respawn 按表重建成员集"
    );
}

#[tokio::test]
async fn test_idle_gc_skips_host_with_never_unload_member() {
    // 连坐保护（§4.8）：任一成员声明 idle_timeout_secs=0（永不卸载）→
    // 宿主整组永不空闲回收（其他成员的 last_used 续命语义之外的第二道防线）。
    let loader = Arc::new(MockLoader::new());
    let mut never = make_light_manifest("never_idle", "python server.py");
    never.lifecycle = Some(agentos_core::traits::PluginLifecycle {
        idle_timeout_secs: Some(0),
    });
    loader.add_manifest(never);
    loader.add_manifest(make_light_manifest("guard_b", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);

    invoker.resolve_host_key(&make_light_manifest("never_idle", "python server.py"));
    invoker.resolve_host_key(&make_light_manifest("guard_b", "python server.py"));
    let host_key = "group:light:1".to_string();
    let live = spawn_long_lived_stdio_client().await;
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));
    invoker
        .last_used
        .write()
        .insert(host_key.clone(), Instant::now() - Duration::from_secs(400));

    invoker.run_idle_gc_pass().await;

    assert!(
        live_arc.read().await.is_alive().await,
        "含永不卸载成员的宿主不得被 GC 回收"
    );
    assert!(invoker.mcp_clients.read().get(&host_key).is_some());
}

#[tokio::test]
async fn test_host_union_fingerprint_sensitivity() {
    // 指纹并集（§4.6）：宿主指纹 = 成员指纹并集 + 成员集本身——
    // 成员代码变更、成员集变化均触发变化；同状态稳定。
    let tmp = tempfile::tempdir().unwrap();
    let dir_a = tmp.path().join("guard_a");
    let dir_b = tmp.path().join("guard_b");
    for d in [&dir_a, &dir_b] {
        std::fs::create_dir_all(d).unwrap();
        std::fs::write(d.join("server.py"), b"print(1)").unwrap();
    }
    let loader = Arc::new(MockLoader::new());
    let manifest_a = make_light_manifest("guard_a", "python server.py");
    let manifest_b = make_light_manifest("guard_b", "python server.py");
    loader.add_manifest(manifest_a.clone());
    loader.add_manifest(manifest_b.clone());
    loader
        .plugin_dirs
        .write()
        .insert("guard_a".to_string(), dir_a.to_string_lossy().into_owned());
    loader
        .plugin_dirs
        .write()
        .insert("guard_b".to_string(), dir_b.to_string_lossy().into_owned());
    let invoker = PluginInvokerImpl::new(loader);

    // 成员 {a}
    invoker.resolve_host_key(&manifest_a);
    let fp_a_only = invoker.host_union_fingerprint("group:light:1");
    // 稳定性：同状态两次计算相同
    assert_eq!(fp_a_only, invoker.host_union_fingerprint("group:light:1"));

    // 成员集变化：加入 b → 指纹变化（成员集本身是指纹的一部分）
    invoker.resolve_host_key(&manifest_b);
    let fp_ab = invoker.host_union_fingerprint("group:light:1");
    assert_ne!(fp_a_only, fp_ab, "新成员加入必须改变宿主指纹");

    // 成员代码变更：a 的文件 mtime 变化 → 指纹变化
    std::thread::sleep(std::time::Duration::from_secs_f64(1.1));
    std::fs::write(dir_a.join("server.py"), b"print(2) # changed").unwrap();
    let fp_ab_changed = invoker.host_union_fingerprint("group:light:1");
    assert_ne!(
        fp_ab, fp_ab_changed,
        "任一成员代码变更必须改变宿主指纹（触发整宿主 respawn）"
    );
}

#[tokio::test]
async fn test_member_set_change_triggers_full_host_respawn() {
    // 指纹并集触发整宿主 respawn（§4.6 行为级）：已 spawn 宿主（假进程入缓存）
    // 在新成员加入分配表后，下次调用 fast path 判 stale → kill 整宿主 → 走 respawn
    // 路径（_host 未落地 → HOST_DIR_NOT_FOUND，证明 respawn 真被触发）。
    let loader = Arc::new(MockLoader::new());
    let manifest_a = make_light_manifest("guard_a", "python server.py");
    let manifest_b = make_light_manifest("guard_b", "python server.py");
    loader.add_manifest(manifest_a.clone());
    loader.add_manifest(manifest_b.clone());
    let invoker = PluginInvokerImpl::new(loader);

    let host_key = invoker.resolve_host_key(&manifest_a);
    // 模拟已 spawn：假进程入缓存 + 指纹已记录（回写 2s 前，绕过 TTL 门）
    let live = spawn_long_lived_stdio_client().await;
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));
    invoker.fingerprints.write().insert(
        host_key.clone(),
        (
            invoker.host_union_fingerprint(&host_key),
            Instant::now() - Duration::from_secs(2),
        ),
    );

    // 新成员 b 加入（成员集变化 → 宿主指纹必变）
    invoker.resolve_host_key(&manifest_b);

    let result = invoker.get_or_create_mcp_client(&manifest_a).await;
    // respawn 被触发：host 命令解析先于 spawn 失败（_host 未落地，HOST_DIR_NOT_FOUND）
    let err = match result {
        Err(e) => e,
        Ok(_) => panic!("成员集变化必须触发整宿主 respawn（走 spawn 路径），不得返回缓存实例"),
    };
    assert_eq!(
        err.code.as_deref(),
        Some("HOST_DIR_NOT_FOUND"),
        "respawn 路径证据：{err}"
    );
    // 旧宿主进程被 kill + 缓存逐出
    assert!(
        !live_arc.read().await.is_alive().await,
        "整宿主 respawn 必须 kill 旧进程"
    );
    assert!(invoker.mcp_clients.read().get(&host_key).is_none());
}

#[tokio::test]
async fn test_lazy_member_boxed_into_live_host_triggers_respawn() {
    // 惰性装箱漂移（缺陷回归）：新 light 成员装箱进**已 spawn 的未满宿主**后，
    // 宿主进程成员集必须重建——否则复用旧进程（成员集在 spawn 时定格）会
    // 报 MCP [-32602] tool not found。
    //
    // 与 test_member_set_change_triggers_full_host_respawn 的差异：该测试把
    // 指纹回写 2s 前绕过 TTL 门；本测试指纹保持**新鲜**（spawn 后立即装箱的
    // 真实时序）——缺陷正是 TTL 门（1s）短路了成员集漂移检测，装箱调用本身
    // 落在 TTL 窗口内，fast path 直接复用旧进程。
    let loader = Arc::new(MockLoader::new());
    let manifest_a = make_light_manifest("guard_a", "python server.py");
    let manifest_b = make_light_manifest("guard_b", "python server.py");
    loader.add_manifest(manifest_a.clone());
    loader.add_manifest(manifest_b.clone());
    let invoker = PluginInvokerImpl::new(loader);

    // 成员 a 装箱 + 模拟已 spawn 宿主（假进程入缓存 + 指纹新鲜记录 +
    // spawn 成员集快照——生产路径下 spawn 时写入，见 spawned_members 注释）
    let host_key = invoker.resolve_host_key(&manifest_a);
    assert_eq!(host_key, "group:light:1");
    let live = spawn_long_lived_stdio_client().await;
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));
    invoker
        .spawned_members
        .write()
        .insert(host_key.clone(), invoker.host_members(&host_key));
    invoker.fingerprints.write().insert(
        host_key.clone(),
        (invoker.host_union_fingerprint(&host_key), Instant::now()),
    );

    // 新成员 b 惰性装箱进同一未满宿主（成员集变化，指纹新鲜窗口内）
    assert_eq!(
        invoker.resolve_host_key(&manifest_b),
        host_key,
        "未满宿主优先塞入（缺陷场景：装箱复用已 spawn 宿主）"
    );

    let result = invoker.get_or_create_mcp_client(&manifest_a).await;
    // respawn 必须被触发：host 命令解析先于 spawn 失败（_host 未落地，
    // HOST_DIR_NOT_FOUND）——不得返回缓存实例（旧进程成员集无 b）
    let err = match result {
        Err(e) => e,
        Ok(_) => panic!("装箱到已存活宿主后必须触发整宿主 respawn，不得复用旧进程"),
    };
    assert_eq!(
        err.code.as_deref(),
        Some("HOST_DIR_NOT_FOUND"),
        "respawn 路径证据：{err}"
    );
    // 旧宿主进程被 kill + 缓存逐出
    assert!(
        !live_arc.read().await.is_alive().await,
        "整宿主 respawn 必须 kill 旧进程"
    );
    assert!(invoker.mcp_clients.read().get(&host_key).is_none());
}

#[tokio::test]
async fn test_spawn_lock_per_host_granularity() {
    // spawn 锁粒度（§4.2 第 4 条）：per-host-key——同宿主成员共享锁条目
    // （串行 spawn），跨宿主各自独立锁条目（并行 spawn）。
    // 锁粒度无外部可观察行为面（真并发 spawn 需 host.py 落地），这里以锁表
    // 键集合 + 同宿主单条目为结构证据；spawn 尝试以 HOST_DIR_NOT_FOUND 失败，
    // 锁条目创建于拿锁阶段，失败路径同样能观察粒度。
    let loader = Arc::new(MockLoader::new());
    let manifest_a = make_light_manifest("guard_a", "python server.py");
    let manifest_b = make_light_manifest("guard_b", "python server.py");
    let solo = make_sidecar_manifest("solo_tool", "definitely_missing_command_98765");
    loader.add_manifest(manifest_a.clone());
    loader.add_manifest(manifest_b.clone());
    loader.add_manifest(solo.clone());
    let invoker = PluginInvokerImpl::new(loader);

    // 同宿主两个成员：spawn 失败（HOST_DIR_NOT_FOUND）但锁条目已按宿主键建立
    let _ = invoker.get_or_create_mcp_client(&manifest_a).await;
    let _ = invoker.get_or_create_mcp_client(&manifest_b).await;
    // 独占宿主：另一个锁条目
    let _ = invoker.get_or_create_mcp_client(&solo).await;

    let mut keys: Vec<String> = invoker.spawn_locks.read().keys().cloned().collect();
    keys.sort();
    assert_eq!(
        keys,
        vec!["group:light:1".to_string(), "plugin:solo_tool".to_string()],
        "同宿主（a/b 共享 group:light:1）单锁条目，跨宿主独立条目"
    );
    // 性质断言：锁条目数 < 已尝试 spawn 的插件数（3 个插件 2 个锁 = 同宿主共享）
    assert!(invoker.spawn_locks.read().len() < 3);
}

#[tokio::test]
async fn test_resolve_group_host_command_contract() {
    // 合宿宿主 spawn 参数契约（§4.2 第 2 条）：`python host.py --group light
    // --slot {n} --members {逗号分隔成员列表}`，工作目录 plugins/shared/_host/，
    // 解释器 = _host 共享 venv（uv 单轨 fail-closed）。
    let tmp = tempfile::tempdir().unwrap();
    // 造 plugins/shared/pipeline/input/guard_a + plugins/shared/_host/.venv 布局
    let member_dir = tmp.path().join("pipeline").join("input").join("guard_a");
    std::fs::create_dir_all(&member_dir).unwrap();
    let host_dir = tmp.path().join("_host");
    let interp = fake_venv(&host_dir, true);
    std::fs::write(host_dir.join("host.py"), b"# host stub").unwrap();

    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "a_guard".to_string(),
        member_dir.to_string_lossy().into_owned(),
    );
    loader.plugin_dirs.write().insert(
        "b_guard".to_string(),
        member_dir.to_string_lossy().into_owned(),
    );
    let invoker = PluginInvokerImpl::new(loader);

    let (cmd, args, workdir) = invoker
        .resolve_group_host_command("light", 3, &["b_guard".to_string(), "a_guard".to_string()])
        .expect("参数契约解析应成功");
    assert_eq!(
        cmd,
        interp.to_string_lossy().into_owned(),
        "解释器 = _host 共享 venv"
    );
    assert_eq!(
        args,
        vec![
            "host.py".to_string(),
            "--group".to_string(),
            "light".to_string(),
            "--slot".to_string(),
            "3".to_string(),
            "--members".to_string(),
            // 成员列表排序（稳定契约：指纹/spawn/观测共用同一序）
            "a_guard,b_guard".to_string(),
        ]
    );
    assert_eq!(workdir, host_dir.to_string_lossy().into_owned());

    // fail-closed：_host 目录缺失 → HOST_DIR_NOT_FOUND
    let empty_loader = Arc::new(MockLoader::new());
    let empty_dir = tempfile::tempdir().unwrap();
    empty_loader.plugin_dirs.write().insert(
        "ghost".to_string(),
        empty_dir.path().to_string_lossy().into_owned(),
    );
    let invoker2 = PluginInvokerImpl::new(empty_loader);
    let err = invoker2
        .resolve_group_host_command("light", 1, &["ghost".to_string()])
        .expect_err("无 _host 目录必须 fail-closed");
    assert_eq!(err.code.as_deref(), Some("HOST_DIR_NOT_FOUND"));

    // fail-closed：_host 存在但共享 venv 缺失 → HOST_VENV_MISSING
    let bare_host = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(bare_host.path().join("_host")).unwrap();
    let bare_member = bare_host.path().join("m");
    std::fs::create_dir_all(&bare_member).unwrap();
    let loader3 = Arc::new(MockLoader::new());
    loader3
        .plugin_dirs
        .write()
        .insert("m".to_string(), bare_member.to_string_lossy().into_owned());
    let invoker3 = PluginInvokerImpl::new(loader3);
    let err3 = invoker3
        .resolve_group_host_command("light", 1, &["m".to_string()])
        .expect_err("共享 venv 缺失必须 fail-closed");
    assert_eq!(err3.code.as_deref(), Some("HOST_VENV_MISSING"));
}

#[test]
fn test_namespaced_tool_name_prefix() {
    // 调用路由前缀（§4.2 第 3 条）：light 成员工具名拼 {plugin_id}. 前缀；
    // 独占宿主（含 light 声明但外部 MCP 形态）无前缀。
    let light = make_light_manifest("pause_guard", "python server.py");
    assert_eq!(namespaced_tool_name(&light, "check"), "pause_guard.check");
    // invoke_entry 本身带点号也整体作为后缀（前缀机制与入口名正交）
    assert_eq!(
        namespaced_tool_name(&light, "pause_guard.check"),
        "pause_guard.pause_guard.check"
    );

    let solo = make_sidecar_manifest("llm_core", "python server.py");
    assert_eq!(
        namespaced_tool_name(&solo, "llm_core.execute"),
        "llm_core.execute"
    );

    let mut ext = make_light_manifest("ext_light", "external");
    ext.mcp = Some(McpConfig {
        transport: McpTransport::StreamableHttp,
        endpoint: Some(McpEndpoint {
            url: Some("http://127.0.0.1:9/mcp".to_string()),
            ..Default::default()
        }),
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: None,
    });
    assert_eq!(
        namespaced_tool_name(&ext, "remote_tool"),
        "remote_tool",
        "外部 MCP 不进合宿组 → 无前缀"
    );
}

#[tokio::test]
async fn test_host_idle_timeout_members_aggregation() {
    // 宿主空闲阈值聚合（§4.8）：任一成员 0 → 永不回收；否则取最严格（最大）。
    // 与 env 突变用例互斥：无生命周期成员的阈值依赖"默认 300"这一全局基线。
    let _serial = ENV_SENSITIVE_LOCK.lock();
    let loader = Arc::new(MockLoader::new());
    let mut never = make_sidecar_manifest("never_idle", "python server.py");
    never.lifecycle = Some(agentos_core::traits::PluginLifecycle {
        idle_timeout_secs: Some(0),
    });
    let mut custom = make_sidecar_manifest("custom_idle", "python server.py");
    custom.lifecycle = Some(agentos_core::traits::PluginLifecycle {
        idle_timeout_secs: Some(42),
    });
    loader.add_manifest(never);
    loader.add_manifest(custom);
    loader.add_manifest(make_sidecar_manifest("default_idle", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);

    // 任一成员永不卸载 → 0（GC 跳过）
    assert_eq!(
        invoker.host_idle_timeout_secs(&["never_idle".to_string(), "custom_idle".to_string()]),
        0
    );
    // 无 0 声明 → 取最大（默认 300 > 42）
    assert_eq!(
        invoker.host_idle_timeout_secs(&["custom_idle".to_string(), "default_idle".to_string()]),
        agentos_core::traits::default_idle_timeout()
    );
    // 单成员默认 → 300
    assert_eq!(
        invoker.host_idle_timeout_secs(&["default_idle".to_string()]),
        agentos_core::traits::default_idle_timeout()
    );
}

#[test]
fn test_light_host_max_members_env_and_default() {
    // 上限配置（§4.5）：默认 6；AGENTOS_LIGHT_HOST_MAX_MEMBERS 覆盖；无效值回退。
    // 仅本测试触碰该环境变量（其他测试全部走 assign_light_host_with 注入），无并发污染。
    std::env::set_var("AGENTOS_LIGHT_HOST_MAX_MEMBERS", "3");
    assert_eq!(light_host_max_members(), 3);
    std::env::set_var("AGENTOS_LIGHT_HOST_MAX_MEMBERS", "0");
    assert_eq!(
        light_host_max_members(),
        LIGHT_HOST_DEFAULT_MAX_MEMBERS,
        "无效值（0）回退默认"
    );
    std::env::set_var("AGENTOS_LIGHT_HOST_MAX_MEMBERS", "not-a-number");
    assert_eq!(
        light_host_max_members(),
        LIGHT_HOST_DEFAULT_MAX_MEMBERS,
        "非数字回退默认"
    );
    std::env::remove_var("AGENTOS_LIGHT_HOST_MAX_MEMBERS");
    assert_eq!(light_host_max_members(), LIGHT_HOST_DEFAULT_MAX_MEMBERS);
}

#[tokio::test]
async fn test_kill_sidecar_if_any_light_kills_whole_host() {
    // disable 窄口 kill 的合宿语义：kill 整宿主（进程所有权单位），分配表保留
    // （reenable 后同组成员回到原宿主 respawn）。
    let loader = Arc::new(MockLoader::new());
    let manifest_a = make_light_manifest("guard_a", "python server.py");
    let manifest_b = make_light_manifest("guard_b", "python server.py");
    loader.add_manifest(manifest_a.clone());
    loader.add_manifest(manifest_b.clone());
    let invoker = PluginInvokerImpl::new(loader);

    let host_key = invoker.resolve_host_key(&manifest_a);
    invoker.resolve_host_key(&manifest_b);
    assert_eq!(host_key, "group:light:1");
    let live = spawn_long_lived_stdio_client().await;
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));

    invoker.kill_sidecar_if_any("guard_a").await;

    assert!(
        !live_arc.read().await.is_alive().await,
        "disable 成员必须 kill 整宿主进程"
    );
    assert!(invoker.mcp_clients.read().get(&host_key).is_none());
    // 分配表保留（窄口语义：不做指纹/分配清理，reenable 后 respawn 回原宿主）
    assert_eq!(
        invoker.light_packing.read().assignments.len(),
        2,
        "窄口 kill 不清分配表"
    );
}

#[tokio::test]
async fn test_force_unload_light_kills_host_keeps_assignments() {
    // force_unload（热重载语境）对 light 成员：kill 整宿主 + 清缓存/指纹/
    // last_used，但**保留分配表**——下次调用按表 respawn 同一宿主（§4.5 第 2
    // 条"respawn 按表重建成员集"）。
    let loader = Arc::new(MockLoader::new());
    let manifest_a = make_light_manifest("guard_a", "python server.py");
    loader.add_manifest(manifest_a.clone());
    let invoker = PluginInvokerImpl::new(loader);

    let host_key = invoker.resolve_host_key(&manifest_a);
    let live = spawn_long_lived_stdio_client().await;
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));
    invoker.touch_last_used(&host_key);

    invoker.force_unload_impl("guard_a").await.unwrap();

    wait_until_killed(&live_arc, "必须 kill 宿主进程").await;
    assert!(invoker.mcp_clients.read().get(&host_key).is_none());
    assert!(invoker.last_used.read().get(&host_key).is_none());
    assert!(
        invoker
            .light_packing
            .read()
            .assignments
            .contains_key("guard_a"),
        "热重载语境保留分配表（respawn 按表重建）"
    );
    // 下次调用仍路由到原宿主（分配粘性）
    assert_eq!(invoker.resolve_host_key(&manifest_a), host_key);
}

#[tokio::test]
async fn test_unload_if_idle_light_reclaims_assignments() {
    // idle 语境回收（unload_host reclaim=true）对 light 成员：整组回收 + 槽位释放
    // （分配表清空）——与 idle GC（run_idle_gc_pass）同一调用形态。
    let loader = Arc::new(MockLoader::new());
    let manifest_a = make_light_manifest("guard_a", "python server.py");
    loader.add_manifest(manifest_a.clone());
    let invoker = PluginInvokerImpl::new(loader);

    let host_key = invoker.resolve_host_key(&manifest_a);
    let live = spawn_long_lived_stdio_client().await;
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));

    invoker
        .unload_host(&host_key, true)
        .await
        .expect("sidecar 插件 idle 卸载应成功");
    assert!(!live_arc.read().await.is_alive().await);
    assert!(
        invoker.light_packing.read().assignments.is_empty(),
        "idle 语境回收即清空分配表（槽位释放）"
    );
}

#[tokio::test]
async fn test_existing_host_key_for_light_unassigned_is_none() {
    // 宿主键反查：light 未分配（从未 spawn）→ None；分配后 → 组键；
    // 独占 sidecar → plugin:{id}；InProcess → None；无 manifest → 独占兜底。
    let loader = Arc::new(MockLoader::new());
    let light = make_light_manifest("guard_a", "python server.py");
    let solo = make_sidecar_manifest("solo_tool", "python server.py");
    let native = make_inprocess_manifest("native_plug");
    loader.add_manifest(light.clone());
    loader.add_manifest(solo);
    loader.add_manifest(native);
    let invoker = PluginInvokerImpl::new(loader);

    assert_eq!(
        invoker.existing_host_key_for("guard_a"),
        None,
        "light 从未分配 = 从未 spawn，无宿主"
    );
    let host_key = invoker.resolve_host_key(&light);
    assert_eq!(
        invoker.existing_host_key_for("guard_a"),
        Some(host_key.clone())
    );
    assert_eq!(
        invoker.existing_host_key_for("solo_tool"),
        Some("plugin:solo_tool".to_string())
    );
    assert_eq!(invoker.existing_host_key_for("native_plug"), None);
    assert_eq!(
        invoker.existing_host_key_for("ghost"),
        Some("plugin:ghost".to_string()),
        "无 manifest 时保守按独占兜底（命中缓存即生效）"
    );
}

/// warmup_sidecar：native（InProcess）无进程模型 → no-op Ok，不触发加载/缓存。
#[tokio::test]
async fn warmup_sidecar_native_is_noop() {
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader.clone());
    let mut m = make_sidecar_manifest("native_x", "server.py");
    m.host_type = HostType::InProcess;
    invoker
        .warmup_sidecar(&m)
        .await
        .expect("native 预热 no-op Ok");
    assert!(
        invoker.mcp_clients.read().is_empty(),
        "no-op 不应产生宿主缓存"
    );
}

/// warmup_sidecar：宿主已缓存存活 → 幂等命中（复用同一实例，不 kill 不 respawn）。
/// boot 预热与首条消息并发场景的行为锚点：single-flight/缓存路径与正常调用同源。
#[tokio::test]
async fn warmup_sidecar_reuses_cached_host_without_respawn() {
    let loader = Arc::new(MockLoader::new());
    let manifest = make_sidecar_manifest("warm_hot", "python server.py");
    loader.add_manifest(manifest.clone());
    let invoker = PluginInvokerImpl::new(loader);

    let live = spawn_long_lived_stdio_client().await;
    let arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert("plugin:warm_hot".to_string(), Arc::clone(&arc));

    invoker
        .warmup_sidecar(&manifest)
        .await
        .expect("缓存命中应 Ok");
    // 性质断言：同一宿主实例被复用（缓存仍是原 Arc）且进程仍活（未 kill）
    let still = invoker
        .mcp_clients
        .read()
        .get("plugin:warm_hot")
        .cloned()
        .expect("幂等命中后缓存条目保留");
    assert!(Arc::ptr_eq(&still, &arc), "幂等命中不得 respawn 换实例");
    assert!(
        arc.read().await.is_alive().await,
        "复用路径不得 kill 存活宿主"
    );
}

/// warmup_sidecar：宿主构建失败（无插件目录 → 解释器缺失）→ Err 传播不吞
/// （调用方据此记日志跳过，懒 spawn 仍是运行期兜底）。
#[tokio::test]
async fn warmup_sidecar_spawn_failure_propagates() {
    let loader = Arc::new(MockLoader::new());
    let manifest = make_sidecar_manifest("warm_missing_venv", "python server.py");
    loader.add_manifest(manifest.clone());
    let invoker = PluginInvokerImpl::new(loader);
    let result = invoker.warmup_sidecar(&manifest).await;
    assert!(
        result.is_err(),
        "venv 缺失的失败必须传播（不吞、不假成功）: {result:?}"
    );
    assert!(
        invoker.mcp_clients.read().is_empty(),
        "失败后不得残留缓存条目"
    );
}

/// preassign_host_key：批量预分配定型装箱——合宿组宿主一次 spawn 即装载完整
/// 成员集的前提。solo 成员不写分配表（纯键构造无状态）。
#[tokio::test]
async fn preassign_host_key_batches_light_packing_before_spawn() {
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);

    // 7 个 light 成员批量预分配（上限走默认 6）：前 6 填满 slot 1，第 7 溢出 slot 2
    let members: Vec<_> = (0..7)
        .map(|i| make_light_manifest(&format!("m{i}"), "python server.py"))
        .collect();
    for m in &members {
        invoker.preassign_host_key(m);
    }
    let assignments = invoker.light_packing.read();
    let slot1 = assignments
        .assignments
        .values()
        .filter(|hk| hk.as_str() == "group:light:1")
        .count();
    let slot2 = assignments
        .assignments
        .values()
        .filter(|hk| hk.as_str() == "group:light:2")
        .count();
    assert_eq!(
        slot1, 6,
        "预分配定型：slot 1 装满 6 成员（非边 spawn 边分配的单成员）"
    );
    assert_eq!(slot2, 1, "溢出成员开 slot 2");
    drop(assignments);

    // 幂等：重复预分配不改归属（粘性）
    for m in &members {
        invoker.preassign_host_key(m);
    }
    assert_eq!(
        invoker.host_members("group:light:1").len(),
        6,
        "重复预分配不改分配表"
    );

    // solo 成员预分配：纯键构造，不写分配表
    let solo = make_sidecar_manifest("solo_x", "python server.py");
    invoker.preassign_host_key(&solo);
    assert!(
        !invoker
            .light_packing
            .read()
            .assignments
            .contains_key("solo_x"),
        "solo 不进 light 分配表"
    );
}

// ── 预热常驻：keep-warm 宿主组豁免 idle GC ──

#[tokio::test]
async fn test_idle_gc_exempts_keep_warm_host_group() {
    // 预热常驻语义：warmup_sidecar 登记的插件所在宿主组不被 idle GC 回收
    // （否则预热被架空，新会话首条消息重新全价冷启动）；非预热宿主照常回收。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_light_manifest("warm_a", "python server.py"));
    loader.add_manifest(make_sidecar_manifest("lazy_b", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);

    // 两组已 spawn 宿主：slot1（light 组，成员 warm_a）与 solo（lazy_b）
    let warm_host = invoker.resolve_host_key(&make_light_manifest("warm_a", "python server.py"));
    assert_eq!(warm_host, "group:light:1");
    let live_warm = Arc::new(tokio::sync::RwLock::new(
        spawn_long_lived_stdio_client().await,
    ));
    invoker
        .mcp_clients
        .write()
        .insert(warm_host.clone(), live_warm.clone());
    invoker.touch_last_used(&warm_host);

    let lazy_host = invoker.resolve_host_key(&make_sidecar_manifest("lazy_b", "python server.py"));
    assert_eq!(lazy_host, "plugin:lazy_b");
    let live_lazy = Arc::new(tokio::sync::RwLock::new(
        spawn_long_lived_stdio_client().await,
    ));
    invoker
        .mcp_clients
        .write()
        .insert(lazy_host.clone(), live_lazy.clone());
    invoker.touch_last_used(&lazy_host);

    // warm_a 登记预热常驻（公开入口）：MockLoader 无真实目录 → spawn 失败
    // 返回 Err，但常驻登记照常生效（warmup_sidecar 契约：失败跳过、登记不变）
    let _ = invoker
        .warmup_sidecar(&make_light_manifest("warm_a", "python server.py"))
        .await;

    // 两组都置为空闲 400s（> 默认阈值 300s）
    for hk in [&warm_host, &lazy_host] {
        invoker
            .last_used
            .write()
            .insert(hk.clone(), Instant::now() - Duration::from_secs(400));
    }

    invoker.run_idle_gc_pass().await;

    assert!(
        live_warm.read().await.is_alive().await,
        "预热常驻宿主组不得被 idle GC 回收"
    );
    assert!(
        !live_lazy.read().await.is_alive().await,
        "非预热宿主组照常整组回收"
    );
    assert!(
        invoker.mcp_clients.read().contains_key(&warm_host),
        "预热宿主缓存保留（下次调用零冷启动）"
    );
    assert!(
        !invoker.mcp_clients.read().contains_key(&lazy_host),
        "回收宿主缓存清空"
    );
}

#[tokio::test]
async fn test_explicit_unload_still_works_on_keep_warm_host() {
    // 豁免只约束 idle GC 的自动回收；显式卸载（force_unload）不受预热常驻约束。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_light_manifest("warm_c", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);

    let host = invoker.resolve_host_key(&make_light_manifest("warm_c", "python server.py"));
    let live = Arc::new(tokio::sync::RwLock::new(
        spawn_long_lived_stdio_client().await,
    ));
    invoker
        .mcp_clients
        .write()
        .insert(host.clone(), live.clone());
    invoker.touch_last_used(&host);
    let _ = invoker
        .warmup_sidecar(&make_light_manifest("warm_c", "python server.py"))
        .await;

    invoker
        .force_unload_impl("warm_c")
        .await
        .expect("显式卸载不受预热常驻豁免约束");
    wait_until_killed(&live, "显式卸载后子进程退出").await;
}

// ── 监控 M3：宿主进程态快照（host_proc_snapshots） ─────────────────────────

fn unconnected_stdio_client() -> SharedMcpClient {
    // 未连接的 stdio client：无子进程 → 判死（stdio 存活期恒持有 child，
    // 缓存中出现无 child 的 stdio 条目 = 未连接/已 kill 的残留，不可用；
    // 真实缓存只会写入 connect 成功的 client，本构造只为覆盖判死路径）
    Arc::new(tokio::sync::RwLock::new(McpClient::new_stdio(
        "python",
        vec!["-c".to_string(), "pass".to_string()],
    )))
}

#[tokio::test]
async fn host_proc_snapshots_empty_invoker_is_empty() {
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    assert!(invoker.host_proc_snapshots().await.is_empty());
}

#[tokio::test]
async fn host_proc_snapshots_reports_member_uptime_and_alive() {
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    invoker
        .mcp_clients
        .write()
        .insert("plugin:demo".to_string(), unconnected_stdio_client());
    invoker
        .host_spawned_at
        .write()
        .insert("plugin:demo".to_string(), Instant::now());

    let snaps = invoker.host_proc_snapshots().await;
    assert_eq!(snaps.len(), 1);
    let s = &snaps[0];
    assert_eq!(s.host_key, "plugin:demo");
    assert!(
        !s.alive,
        "缓存中无 child 的 stdio client = 未连接/已 kill 残留，应判死"
    );
    assert_eq!(s.pid, None);
    assert!(s.uptime_secs.is_some(), "有 spawn 记账应有 uptime");
    // 独占宿主：成员 = 宿主键前缀解析
    assert_eq!(s.plugin_ids, vec!["demo".to_string()]);
}

#[tokio::test]
async fn host_proc_snapshots_light_group_lists_all_members() {
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    invoker
        .mcp_clients
        .write()
        .insert("group:light:1".to_string(), unconnected_stdio_client());
    // 轻量组成员 = packing 分配表（plugin_id → 宿主键）
    invoker
        .light_packing
        .write()
        .assignments
        .insert("member_a".to_string(), "group:light:1".to_string());
    invoker
        .light_packing
        .write()
        .assignments
        .insert("member_b".to_string(), "group:light:1".to_string());

    let snaps = invoker.host_proc_snapshots().await;
    assert_eq!(snaps.len(), 1);
    let mut ids = snaps[0].plugin_ids.clone();
    ids.sort();
    assert_eq!(ids, vec!["member_a".to_string(), "member_b".to_string()]);
}

#[tokio::test]
async fn host_proc_snapshots_without_spawn_record_omits_uptime() {
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    invoker
        .mcp_clients
        .write()
        .insert("plugin:x".to_string(), unconnected_stdio_client());
    let snaps = invoker.host_proc_snapshots().await;
    assert_eq!(snaps.len(), 1);
    assert_eq!(snaps[0].uptime_secs, None, "无 spawn 记账 uptime 应为 None");
}

#[tokio::test]
async fn emit_lifecycle_error_fans_out_to_bus_with_context() {
    // OnError 旁路广播契约：emit 后总线订阅者收到事件，ctx 携带 plugin_id /
    // error / error_code（监控页"插件报错"计数的数据源）。
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    let bus = Arc::new(HookEventBus::new(32));
    invoker.set_hook_bus(bus.clone());
    let mut rx = bus.subscribe();

    invoker.emit_lifecycle_error(
        "my_plugin",
        "tool execution failed",
        Some("MCP_TOOL_CALL_FAILED"),
    );

    let ev = tokio::time::timeout(std::time::Duration::from_millis(500), rx.recv())
        .await
        .expect("应在超时前收到 OnError 事件")
        .expect("总线未关闭");
    assert_eq!(ev.hook, LifecycleHook::OnError);
    assert_eq!(ev.ctx.get("plugin_id"), Some(&json!("my_plugin")));
    assert_eq!(ev.ctx.get("error"), Some(&json!("tool execution failed")));
    assert_eq!(
        ev.ctx.get("error_code"),
        Some(&json!("MCP_TOOL_CALL_FAILED"))
    );
}

#[tokio::test]
async fn emit_lifecycle_error_without_bus_is_noop() {
    // bus 未注入（如单测环境）时 no-op 不 panic——best-effort 观察路径契约。
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    invoker.emit_lifecycle_error("my_plugin", "boom", None);
}

#[test]
fn pipeline_error_params_counts_err_and_ok_error_field() {
    // Err（崩溃/MCP/解析）→ 报错；Ok 携带 error 字段（业务失败）→ 同样报错；
    // Ok 无 error → None 不发事件。
    let err = Err(PluginError {
        message: "plugin process crashed: p".to_string(),
        code: Some("PLUGIN_CRASHED".to_string()),
        source: None,
    });
    let (msg, code) = pipeline_error_params(&err).expect("Err 应计报错");
    assert_eq!(msg, "plugin process crashed: p");
    assert_eq!(code.as_deref(), Some("PLUGIN_CRASHED"));

    let ok_err = Ok(PluginResult::default().with_error(PluginError {
        message: "step blew up".to_string(),
        code: None,
        source: None,
    }));
    let (msg, code) = pipeline_error_params(&ok_err).expect("Ok error 字段应计报错");
    assert_eq!(msg, "step blew up");
    assert_eq!(code, None);

    assert!(pipeline_error_params(&Ok(PluginResult::default())).is_none());
}

#[test]
fn tool_error_params_counts_success_false_only() {
    // success=false（isError/失败信封）→ 报错，error 缺省给兜底文案；
    // success=true → None。两个失败形态是计数器的行为契约。
    let failed = Ok(ToolExecutionResult::failure("container_create_failed"));
    let (msg, code) = tool_error_params(&failed).expect("success=false 应计报错");
    assert_eq!(msg, "container_create_failed");
    assert_eq!(code, None);

    let failed_no_msg = Ok(ToolExecutionResult {
        success: false,
        data: json!({}),
        error: None,
        duration_ms: None,
        metadata: None,
    });
    let (msg, _) = tool_error_params(&failed_no_msg).expect("success=false 无 error 也计");
    assert_eq!(msg, "tool execution failed");

    assert!(tool_error_params(&Ok(ToolExecutionResult::success(json!({})))).is_none());
    assert!(tool_error_params(&Err(PluginError {
        message: "MCP tool call failed".to_string(),
        code: Some("MCP_TOOL_CALL_FAILED".to_string()),
        source: None,
    }))
    .is_some());
}

// ── 反向调用连接身份 = 进程身份（G6 组主体配套，2026-09-03 工具面置空事故修复）──

#[test]
fn test_scoped_router_identity_light_member_uses_host_key() {
    // 合宿成员身份 = 宿主键（进程即主体），不随组重生锚定成员漂移。
    let m = make_light_manifest("pipeline_tool_schema", "python server.py");
    assert_eq!(
        super::scoped_router_identity(&m, "group:light:3"),
        "group:light:3"
    );
}

#[test]
fn test_scoped_router_identity_exclusive_uses_manifest_id() {
    // 独占插件身份 = manifest.id（现状语义；注意不是 plugin: 前缀宿主键）。
    let m = make_sidecar_manifest("llm_core", "python server.py");
    assert_eq!(
        super::scoped_router_identity(&m, "plugin:llm_core"),
        "llm_core"
    );
}

#[test]
fn test_scoped_router_identity_external_mcp_not_grouped() {
    // host_group=light 但外部 stdio 命令 → 不进合宿，身份仍 = manifest.id。
    let mut m = make_light_manifest("external_stdio", "unused entry");
    m.mcp = Some(McpConfig {
        transport: McpTransport::Stdio,
        endpoint: Some(McpEndpoint {
            command: Some("npx".to_string()),
            args: vec!["-y".to_string()],
            ..Default::default()
        }),
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: None,
    });
    assert_eq!(
        super::scoped_router_identity(&m, "group:light:1"),
        "external_stdio"
    );
}

// ── group_granted_capabilities：组 = 授权主体，声明 = 成员归并 ──

#[test]
fn test_group_grants_union_sorted_deduped() {
    // 全员声明 → 并集排序去重；同组多成员交叉声明去重。
    let loader = Arc::new(MockLoader::new());
    let mut a = make_light_manifest("ga", "python a.py");
    a.granted_capabilities = vec!["tool-surface".to_string()];
    let mut b = make_light_manifest("gb", "python b.py");
    b.granted_capabilities = vec!["pipeline-state".to_string(), "tool-surface".to_string()];
    loader.add_manifest(a);
    loader.add_manifest(b);

    let invoker = PluginInvokerImpl::new(loader);
    assert_eq!(
        invoker.assign_light_host_with("ga", "light", 6),
        "group:light:1"
    );
    assert_eq!(
        invoker.assign_light_host_with("gb", "light", 6),
        "group:light:1"
    );

    let grants = invoker
        .group_granted_capabilities("group:light:1")
        .expect("全员声明应得 Some");
    assert_eq!(
        grants,
        vec!["pipeline-state".to_string(), "tool-surface".to_string()]
    );
}

#[test]
fn test_group_grants_any_undeclared_member_is_none() {
    // 任一成员未声明 = 组未声明（沿用未声明语义开关），存量不因合宿收窄。
    let loader = Arc::new(MockLoader::new());
    let mut a = make_light_manifest("ga", "python a.py");
    a.granted_capabilities = vec!["tool-surface".to_string()];
    let b = make_light_manifest("gb", "python b.py"); // 未声明
    loader.add_manifest(a);
    loader.add_manifest(b);

    let invoker = PluginInvokerImpl::new(loader);
    invoker.assign_light_host_with("ga", "light", 6);
    invoker.assign_light_host_with("gb", "light", 6);

    assert!(invoker
        .group_granted_capabilities("group:light:1")
        .is_none());
}

#[test]
fn test_group_grants_missing_member_manifest_is_none() {
    // 装箱表有成员但 manifest 缺失（热发现窗口/被禁用）→ 组按未声明处理。
    let loader = Arc::new(MockLoader::new());
    let mut a = make_light_manifest("ga", "python a.py");
    a.granted_capabilities = vec!["tool-surface".to_string()];
    loader.add_manifest(a);

    let invoker = PluginInvokerImpl::new(loader);
    invoker.assign_light_host_with("ga", "light", 6);
    invoker.assign_light_host_with("ghost", "light", 6); // 无 manifest

    assert!(invoker
        .group_granted_capabilities("group:light:1")
        .is_none());
}

#[test]
fn test_group_grants_non_group_or_empty_keys_are_none() {
    // 非 light 组键 / 无成员组键 → None（独占键根本不该进组分支，双保险）。
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);
    assert!(invoker.group_granted_capabilities("plugin:ga").is_none());
    assert!(invoker
        .group_granted_capabilities("group:heavy:1")
        .is_none());
    assert!(invoker
        .group_granted_capabilities("group:light:9")
        .is_none());
}

#[test]
fn test_group_grants_follows_live_packing_no_stale_snapshot() {
    // 性质断言：归并按当前装箱表现算，成员集变化即时生效（无快照失效面）。
    let loader = Arc::new(MockLoader::new());
    let mut a = make_light_manifest("ga", "python a.py");
    a.granted_capabilities = vec!["tool-surface".to_string()];
    let mut b = make_light_manifest("gb", "python b.py");
    b.granted_capabilities = vec!["pipeline-state".to_string()];
    loader.add_manifest(a);
    loader.add_manifest(b);

    let invoker = PluginInvokerImpl::new(loader);
    invoker.assign_light_host_with("ga", "light", 6);
    assert_eq!(
        invoker.group_granted_capabilities("group:light:1"),
        Some(vec!["tool-surface".to_string()])
    );

    // gb 装箱进同组后，归并立即包含其声明。
    invoker.assign_light_host_with("gb", "light", 6);
    assert_eq!(
        invoker.group_granted_capabilities("group:light:1"),
        Some(vec![
            "pipeline-state".to_string(),
            "tool-surface".to_string()
        ])
    );
}

#[tokio::test]
async fn test_run_freeze_dump_writes_report_with_fake_spy() {
    // 冻结取证契约：假 py-spy 替身（外部进程是可替身依赖）——产物落盘、
    // 文件名带 plugin/pid、正文含元信息头与替身 stdout。
    let dir = tempfile::tempdir().unwrap();
    let fake = dir.path().join(if cfg!(windows) {
        "fake-spy.cmd"
    } else {
        "fake-spy"
    });
    if cfg!(windows) {
        std::fs::write(&fake, "@echo off\r\necho Thread 0x1 (idle): run_loop\r\n").unwrap();
    } else {
        std::fs::write(&fake, "#!/bin/sh\necho 'Thread 0x1 (idle): run_loop'\n").unwrap();
    }
    let out_dir = dir.path().join("dumps");
    let got = PluginInvokerImpl::run_freeze_dump(
        fake.to_str().unwrap(),
        "llm_service",
        4242,
        42,
        &out_dir,
        10,
    )
    .await;
    let path = got.expect("假 spy 成功应返回产物路径");
    let name = path.file_name().unwrap().to_string_lossy().to_string();
    assert!(
        name.starts_with("llm_service_") && name.ends_with("_pid4242.txt"),
        "产物名: {name}"
    );
    let body = std::fs::read_to_string(&path).unwrap();
    assert!(body.contains("# plugin: llm_service"), "元信息头缺失");
    assert!(body.contains("# pid: 4242"));
    assert!(body.contains("# heartbeat_stale_secs: 42"));
    assert!(body.contains("run_loop"), "应包含替身 py-spy 的 stdout");
}

#[tokio::test]
async fn test_run_freeze_dump_missing_spy_degrades_to_none() {
    // 尽力而为契约：spy 二进制不存在 → None（降级告警），不 panic 不长阻塞，
    // 调用点随后照常 force_unload 自愈。
    let dir = tempfile::tempdir().unwrap();
    let got = PluginInvokerImpl::run_freeze_dump(
        "no-such-py-spy-binary-xyz",
        "llm_service",
        1,
        31,
        dir.path(),
        10,
    )
    .await;
    assert!(got.is_none(), "spy 缺失应降级返回 None");
}

#[tokio::test]
async fn test_run_freeze_dump_timeout_degrades_to_none() {
    // 挂死 spy（超过 timeout 不退出）→ None 降级，不留产物（回收硬时限优先）。
    let dir = tempfile::tempdir().unwrap();
    let slow = dir.path().join(if cfg!(windows) {
        "slow-spy.cmd"
    } else {
        "slow-spy"
    });
    if cfg!(windows) {
        std::fs::write(&slow, "@echo off\r\nping -n 30 127.0.0.1 >nul\r\n").unwrap();
    } else {
        std::fs::write(&slow, "#!/bin/sh\nsleep 30\n").unwrap();
    }
    let got = PluginInvokerImpl::run_freeze_dump(
        slow.to_str().unwrap(),
        "llm_service",
        7,
        35,
        dir.path(),
        1,
    )
    .await;
    assert!(got.is_none(), "超时应降级返回 None");
    let leftover: Vec<_> = std::fs::read_dir(dir.path())
        .unwrap()
        .filter_map(|e| e.ok())
        .map(|e| e.file_name().to_string_lossy().to_string())
        .filter(|n| n.contains("llm_service"))
        .collect();
    assert!(leftover.is_empty(), "超时不得残留半截产物: {leftover:?}");
}

// ── P12：in-flight 门 + 声明式调用超时（ADR 2026-09-07）──────────────────

use agentos_core::traits::default_capability_timeout_ms;

/// 带 lifecycle 阈值声明的 sidecar manifest（threshold 确定性：不读环境变量）。
fn make_p12_manifest(id: &str, idle_timeout_secs: u64) -> PluginManifest {
    let mut m = make_sidecar_manifest(id, "python server.py");
    m.lifecycle = Some(PluginLifecycle {
        idle_timeout_secs: Some(idle_timeout_secs),
    });
    m
}

/// 把宿主 last_used 回拨（伪造空闲超期）。
fn backdate_idle(invoker: &PluginInvokerImpl, host_key: &str, secs: u64) {
    invoker.last_used.write().insert(
        host_key.to_string(),
        Instant::now() - Duration::from_secs(secs),
    );
}

#[tokio::test]
async fn test_p12_idle_gc_skips_host_with_inflight_calls() {
    // in-flight>0 且空闲超期 → 不回收（宿主键条目保留 = unload_host 未执行）
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_p12_manifest("p12a", 300));
    let invoker = PluginInvokerImpl::new(loader);
    let host_key = "plugin:p12a";
    backdate_idle(&invoker, host_key, 400);

    let _guard = invoker.enter_inflight(host_key);
    assert_eq!(invoker.host_inflight(host_key), 1);

    invoker.run_idle_gc_pass().await;

    assert!(
        invoker.last_used.read().contains_key(host_key),
        "in-flight>0 时超阈值宿主不得被回收"
    );
}

#[tokio::test]
async fn test_p12_idle_gc_unloads_host_after_calls_settle() {
    // 同宿主计数归零后，超阈值 → 正常回收（last_used 条目被 unload_host 清除）
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_p12_manifest("p12b", 300));
    let invoker = PluginInvokerImpl::new(loader);
    let host_key = "plugin:p12b";
    backdate_idle(&invoker, host_key, 400);

    {
        let _guard = invoker.enter_inflight(host_key);
        assert_eq!(invoker.host_inflight(host_key), 1);
    } // guard drop → 计数回落
    assert_eq!(invoker.host_inflight(host_key), 0, "调用结束计数必须归零");

    invoker.run_idle_gc_pass().await;

    assert!(
        !invoker.last_used.read().contains_key(host_key),
        "计数归零后超阈值宿主应被正常回收"
    );
    assert_eq!(
        invoker.host_inflight(host_key),
        0,
        "回收清理后计数条目不残留"
    );
}

#[tokio::test(start_paused = true)]
async fn test_p12_capability_timeout_returns_structured_error() {
    // fake clock（tokio paused time）：到点前不完成，到点返回 CAPABILITY_TIMEOUT
    let mut fut = Box::pin(with_capability_timeout::<()>(
        Some(5_000),
        "p12_plugin",
        "p12_tool",
        std::future::pending(),
    ));
    tokio::select! {
        _ = fut.as_mut() => panic!("超时点前不应完成"),
        _ = tokio::time::advance(Duration::from_millis(4_999)) => {}
    }
    let err = fut.await.unwrap_err();
    assert_eq!(err.code.as_deref(), Some("CAPABILITY_TIMEOUT"));
    assert!(err.message.contains("p12_plugin"), "message 须含插件 id");
    assert!(err.message.contains("p12_tool"), "message 须含工具名");
    assert!(
        err.message.contains("5000ms"),
        "message 须含时长: {}",
        err.message
    );
}

#[tokio::test(start_paused = true)]
async fn test_p12_capability_timeout_settles_inflight_count() {
    // 超时放弃内层 future → 内层持有的 in-flight guard 随 drop 回落
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);
    let host_key = "plugin:p12c";

    // 模拟 attempt_sidecar_* 内部形态：guard 持于内层 future，调用 pend。
    let mut fut = Box::pin(with_capability_timeout(Some(1_000), "p12c", "t", async {
        let _guard = invoker.enter_inflight(host_key);
        std::future::pending::<Result<(), PluginError>>().await
    }));
    tokio::select! {
        _ = fut.as_mut() => panic!("超时点前不应完成"),
        _ = tokio::time::advance(Duration::from_millis(999)) => {}
    }
    assert_eq!(
        invoker.host_inflight(host_key),
        1,
        "超时点前调用在飞，计数必须在悬"
    );

    drop(fut); // 超时放弃内层 future（与 tokio::time::timeout 到点行为同构）
    assert_eq!(
        invoker.host_inflight(host_key),
        0,
        "超时放弃内层 future 后计数必须回落"
    );
}

#[test]
fn test_p12_tool_timeout_ms_precedence() {
    // ① 工具声明生效
    let mut m = make_sidecar_manifest("p12d", "python server.py");
    m.capabilities.tools = vec![p12_tool_cap("t", Some(123_456))];
    m.mcp = Some(McpConfig {
        transport: McpTransport::Stdio,
        endpoint: None,
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: Some(9),
    });
    assert_eq!(tool_timeout_ms(&m, "t"), Some(123_456), "工具声明值生效");

    // ② 未声明工具 → 插件级 request_timeout_secs 兜底（秒→毫秒）
    let mut m2 = make_sidecar_manifest("p12e", "python server.py");
    m2.capabilities.tools = vec![p12_tool_cap("t", None)];
    m2.mcp = Some(McpConfig {
        transport: McpTransport::Stdio,
        endpoint: None,
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: Some(1_800),
    });
    assert_eq!(
        tool_timeout_ms(&m2, "t"),
        Some(1_800_000),
        "插件级声明兜底（秒→毫秒）"
    );

    // ③ 全未声明 → 内核默认 300_000ms
    let m3 = make_sidecar_manifest("p12f", "python server.py");
    assert_eq!(
        tool_timeout_ms(&m3, "nope"),
        Some(default_capability_timeout_ms())
    );

    // ④ Some(0) = 显式豁免
    let mut m4 = make_sidecar_manifest("p12g", "python server.py");
    m4.capabilities.tools = vec![p12_tool_cap("t", Some(0))];
    assert_eq!(tool_timeout_ms(&m4, "t"), None, "Some(0) = 豁免总时长超时");

    // ⑤ 按工具名隔离：未命中声明的工具走兜底，不吃别的工具的声明
    let mut m5 = make_sidecar_manifest("p12h", "python server.py");
    m5.capabilities.tools = vec![p12_tool_cap("fast", Some(1_000))];
    assert_eq!(
        tool_timeout_ms(&m5, "other"),
        Some(default_capability_timeout_ms())
    );
}

/// P12 测试用 ToolCapability 构造（其余字段 None 缺省形态）。
fn p12_tool_cap(name: &str, timeout_ms: Option<u64>) -> agentos_core::traits::ToolCapability {
    agentos_core::traits::ToolCapability {
        name: name.to_string(),
        description: None,
        input_schema: None,
        output_schema: None,
        category: None,
        ui: None,
        render: None,
        smoke: None,
        timeout_ms,
    }
}

#[test]
fn test_p12_manifest_timeout_ms_serde_roundtrip() {
    // 声明值经真实 serde 面生效（序列化出口 + 反序列化入口）
    let mut m = make_sidecar_manifest("p12i", "python server.py");
    m.capabilities.tools = vec![p12_tool_cap("t", Some(60_000))];
    let v = serde_json::to_value(&m).unwrap();
    assert_eq!(v["capabilities"]["tools"][0]["timeout_ms"], 60_000);
    let round: PluginManifest = serde_json::from_value(v).unwrap();
    assert_eq!(round.capabilities.tools[0].timeout_ms, Some(60_000));

    // 存量 manifest 无该字段 → serde default 解析为 None（字段只增不删，兼容）
    let mut m2 = make_sidecar_manifest("p12j", "python server.py");
    m2.capabilities.tools = vec![p12_tool_cap("t", None)];
    let v2 = serde_json::to_value(&m2).unwrap();
    assert!(
        v2["capabilities"]["tools"][0].get("timeout_ms").is_none(),
        "None 不序列化（skip_serializing_if）"
    );
    let round2: PluginManifest = serde_json::from_value(v2).unwrap();
    assert_eq!(round2.capabilities.tools[0].timeout_ms, None);
}

#[tokio::test]
async fn test_p12_capability_timeout_none_is_passthrough() {
    // 豁免（None）：不包界——内层 future 正常完成，语义零改变
    let result =
        with_capability_timeout(None, "p12k", "t", async { Ok::<(), PluginError>(()) }).await;
    assert!(result.is_ok(), "豁免路径透传内层结果");
}

// ── P22：in-flight 计数下沉统一入口（补 79cb1dc72 漏斗盲区，ADR 2026-09-07）──

/// llm_service 形态的 System sidecar manifest：工具声明为空（服务式工具经
/// tool-executor 反向调用）、`mcp.request_timeout_secs` 自声明流式等待界。
fn make_p22_llm_shape_manifest(id: &str) -> PluginManifest {
    let mut m = make_sidecar_manifest(id, "python server.py");
    m.plugin_type = PluginType::System;
    m.mcp = Some(McpConfig {
        transport: McpTransport::Stdio,
        endpoint: None,
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: Some(3600),
    });
    m
}

#[tokio::test]
async fn test_p22_system_direct_call_blocks_reclaim_at_unified_entry() {
    // System 插件（服务式直调形态，llm_service 同构）调用进行中：统一入口
    // 计数 >0 → 超阈值 GC 走 skipped 路径不回收；计数回落 → 正常回收。
    let loader = Arc::new(MockLoader::new());
    let m = make_p22_llm_shape_manifest("llm_like");
    let host_key = solo_host_key(&m.id);
    loader.add_manifest(m.clone());
    let invoker = PluginInvokerImpl::new(loader);

    // 统一入口计入（与 invoke_pipeline_plugin/invoke_tool Sidecar 分支同代码）
    assert_eq!(tool_timeout_ms(&m, "llm.complete_stream"), Some(3_600_000));
    let (counted_key, guard) = invoker.enter_sidecar_inflight(&m);
    assert_eq!(counted_key, host_key, "System 独占宿主键 = solo_host_key");
    assert_eq!(invoker.host_inflight(&host_key), 1);

    backdate_idle(&invoker, &host_key, 400);
    invoker.run_idle_gc_pass().await;
    assert!(
        invoker.last_used.read().contains_key(&host_key),
        "流式直调进行中（计数=1）超阈值宿主必须走 skipped 路径不回收"
    );

    drop(guard); // 流结束/断连 → RAII 回落
    assert_eq!(invoker.host_inflight(&host_key), 0, "流结束后计数必须归零");
    invoker.run_idle_gc_pass().await;
    assert!(
        !invoker.last_used.read().contains_key(&host_key),
        "计数归零后超阈值宿主应被正常回收"
    );
}

#[tokio::test(start_paused = true)]
async fn test_p22_streaming_call_count_spans_whole_stream() {
    // 流式调用全生命周期计数（19:09 / 07:57 两次卡死的关键窗口）：流进行中
    // （跨多个 GC 周期）计数悬置不回落、回收被拦截；流完全结束才回落。
    // fake clock：tokio paused time 驱动超时界；空闲判定由注入的 backdate
    // （std Instant 直改）驱动——无零延迟 mock。
    let loader = Arc::new(MockLoader::new());
    let m = make_p22_llm_shape_manifest("llm_stream");
    let host_key = solo_host_key(&m.id);
    loader.add_manifest(m.clone());
    let invoker = Arc::new(PluginInvokerImpl::new(loader));

    let (stream_end_tx, stream_end_rx) = tokio::sync::oneshot::channel::<()>();
    let call = tokio::spawn({
        let invoker = Arc::clone(&invoker);
        let m = m.clone();
        async move {
            // 统一入口形态：内核超时包界（插件自声明 3600s）内持 guard，
            // MCP 响应在流完全结束才返回（oneshot 模拟流生命周期）。
            with_capability_timeout(
                tool_timeout_ms(&m, "llm.complete_stream"),
                &m.id,
                "llm.complete_stream",
                async {
                    let (_key, _guard) = invoker.enter_sidecar_inflight(&m);
                    let _stream_alive = stream_end_rx.await;
                    Ok(())
                },
            )
            .await
        }
    });

    // 流建立（推进假时钟等 guard 出现，非零延迟）
    for _ in 0..1000 {
        tokio::time::advance(Duration::from_millis(1)).await;
        if invoker.host_inflight(&host_key) == 1 {
            break;
        }
    }
    assert_eq!(
        invoker.host_inflight(&host_key),
        1,
        "流式调用建立后计数必须悬置"
    );

    // 流进行中跨多个 GC 周期（空闲已超阈值）：每个周期都走 skipped 不回收
    for cycle in 0..3 {
        backdate_idle(&invoker, &host_key, 400 + cycle * 30);
        invoker.run_idle_gc_pass().await;
        assert!(
            invoker.last_used.read().contains_key(&host_key),
            "流进行中第 {} 个 GC 周期不得回收宿主",
            cycle + 1
        );
        tokio::time::advance(Duration::from_secs(30)).await;
    }

    // 流完全结束 → guard 随 future drop 回落 → GC 正常回收
    stream_end_tx.send(()).ok();
    call.await.unwrap().unwrap();
    assert_eq!(
        invoker.host_inflight(&host_key),
        0,
        "流完全结束后计数必须回落"
    );
    backdate_idle(&invoker, &host_key, 400);
    invoker.run_idle_gc_pass().await;
    assert!(
        !invoker.last_used.read().contains_key(&host_key),
        "流结束后超阈值宿主应被正常回收"
    );
}

// ── D3：freeze 取证 dump 保留上限（写入时清扫只留最新 10 份）──────────────

#[test]
fn sweep_freeze_dump_dir_keeps_latest_n_deletes_oldest() {
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("freeze_dumps");
    std::fs::create_dir_all(&dir).unwrap();
    // 12 份 dump，写时间逐份 +60s 排开（显式 set_modified，不依赖写盘时戳精度）
    let base = std::time::SystemTime::now();
    for i in 0..12u64 {
        let p = dir.join(format!("pluginA_{:012}_pid1.txt", 1_000_000 + i));
        std::fs::write(&p, "dump").unwrap();
        let f = std::fs::OpenOptions::new().write(true).open(&p).unwrap();
        f.set_modified(base + std::time::Duration::from_secs(i * 60))
            .unwrap();
    }
    // 对照插件：同样参与总上限竞争（跨插件共用一个总上限）
    let p_other = dir.join("pluginB_000000000000_pid2.txt");
    std::fs::write(&p_other, "dump").unwrap();
    let f = std::fs::OpenOptions::new()
        .write(true)
        .open(&p_other)
        .unwrap();
    f.set_modified(base - std::time::Duration::from_secs(3600))
        .unwrap();

    super::PluginInvokerImpl::sweep_freeze_dump_dir(&dir, 10);

    let mut remaining: Vec<String> = std::fs::read_dir(&dir)
        .unwrap()
        .filter_map(|e| e.ok())
        .map(|e| e.file_name().to_string_lossy().to_string())
        .collect();
    remaining.sort();
    assert_eq!(remaining.len(), 10, "清扫后必须恰好剩 10 份: {remaining:?}");
    // 最旧的 3 份（pluginB + pluginA 前两份）被删，最新 10 份保留
    assert!(
        !remaining.contains(&"pluginB_000000000000_pid2.txt".to_string()),
        "最旧文件必须被清扫: {remaining:?}"
    );
    assert!(
        remaining.contains(&"pluginA_000001000011_pid1.txt".to_string()),
        "最新一份必须保留: {remaining:?}"
    );
    // 幂等：再扫一次无变化（性质断言）
    super::PluginInvokerImpl::sweep_freeze_dump_dir(&dir, 10);
    assert_eq!(std::fs::read_dir(&dir).unwrap().count(), 10, "重复清扫幂等");
}

#[test]
fn sweep_freeze_dump_dir_noop_under_limit_or_missing_dir() {
    // 未超限：一个都不删
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("freeze_dumps");
    std::fs::create_dir_all(&dir).unwrap();
    for i in 0..3u64 {
        std::fs::write(dir.join(format!("d_{i}.txt")), "dump").unwrap();
    }
    super::PluginInvokerImpl::sweep_freeze_dump_dir(&dir, 10);
    assert_eq!(std::fs::read_dir(&dir).unwrap().count(), 3, "未超限零删除");

    // 目录不存在：静默返回不 panic
    super::PluginInvokerImpl::sweep_freeze_dump_dir(&tmp.path().join("nope"), 10);
}

// ═══════════════════════════════════════════════════════════════════════════
// 分支补测（既有惯例：外部 MCP server 用本地环回 mock HTTP 替身，子进程用
// 长驻/速死替身，时序用回写时间戳，不用真实长睡眠）
// ═══════════════════════════════════════════════════════════════════════════

/// 本地 mock HTTP MCP server（`list_plugin_tools_returns_bounded_error_*` 内
/// 延迟 mock 的通用化）：非 tools/call 请求（initialize / notifications /
/// tools/list）回 `{"ok":true}`；tools/call 延迟 `tools_call_delay` 后回 MCP
/// content 信封，`content[0].text` = `tools_call_text`（业务载荷由用例决定）。
/// 返回监听 url（127.0.0.1 随机端口，listener 随 task 结束关闭）。
async fn spawn_mock_http_mcp(tools_call_text: String, tools_call_delay: Duration) -> String {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    fn subseq(hay: &[u8], needle: &[u8]) -> Option<usize> {
        hay.windows(needle.len()).position(|w| w == needle)
    }
    fn content_length(headers: &[u8]) -> usize {
        String::from_utf8_lossy(headers)
            .to_ascii_lowercase()
            .lines()
            .find_map(|l| l.strip_prefix("content-length:"))
            .and_then(|v| v.trim().parse().ok())
            .unwrap_or(0)
    }
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let tools_payload = serde_json::json!({
        "jsonrpc": "2.0", "id": 1,
        "result": {"content": [{"type": "text", "text": tools_call_text}], "isError": false}
    })
    .to_string();
    let generic_payload = r#"{"jsonrpc":"2.0","id":1,"result":{"ok":true}}"#.to_string();
    tokio::spawn(async move {
        loop {
            let Ok((mut sock, _)) = listener.accept().await else {
                return;
            };
            let tools_payload = tools_payload.clone();
            let generic_payload = generic_payload.clone();
            tokio::spawn(async move {
                loop {
                    let mut data = Vec::new();
                    let mut tmp = [0u8; 4096];
                    loop {
                        match sock.read(&mut tmp).await {
                            Ok(0) | Err(_) => return,
                            Ok(n) => {
                                data.extend_from_slice(&tmp[..n]);
                                if let Some(hdr_end) = subseq(&data, b"\r\n\r\n") {
                                    let cl = content_length(&data[..hdr_end]);
                                    if data.len() >= hdr_end + 4 + cl {
                                        break;
                                    }
                                }
                            }
                        }
                    }
                    let payload = if subseq(&data, b"\"tools/call\"").is_some() {
                        tokio::time::sleep(tools_call_delay).await;
                        tools_payload.clone()
                    } else {
                        generic_payload.clone()
                    };
                    let out = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
                        payload.len(),
                        payload
                    );
                    if sock.write_all(out.as_bytes()).await.is_err() {
                        return;
                    }
                }
            });
        }
    });
    url
}

/// HTTP pipeline manifest（mock HTTP MCP 端到端用）：Pipeline 类型 + invoke_entry。
fn make_mock_http_pipeline_manifest(id: &str, url: &str) -> PluginManifest {
    let mut m = make_http_manifest(id, url);
    m.plugin_type = PluginType::Pipeline;
    m.invoke_entry = Some(format!("{id}.execute"));
    m
}

#[tokio::test]
async fn invoke_tool_sidecar_success_wraps_business_data() {
    // tools/call 返回纯业务 dict → 归一层包 success 信封（attempt_sidecar_tool
    // Ok 主路径的端到端）；调用结束 in-flight 计数必须回落（RAII guard 随
    // future 完成释放）。
    let url =
        spawn_mock_http_mcp(r#"{"result":[1,2]}"#.to_string(), Duration::from_millis(0)).await;
    let loader = Arc::new(MockLoader::new());
    let mut m = make_http_manifest("tool_ok", &url);
    m.capabilities.tools = vec![p12_tool_cap("biz", None)];
    loader.add_manifest(m);
    let invoker = PluginInvokerImpl::new(loader);

    let r = invoker
        .invoke_tool("tool_ok", "biz", &json!({"x": 1}))
        .await
        .expect("业务 dict 必须归一为 success");
    assert!(r.success, "got: {:?}", r);
    assert_eq!(r.data, json!({"result":[1,2]}), "业务数据原样进 data");
    assert_eq!(r.error, None);
    assert_eq!(r.duration_ms, None, "非信封形态不得伪造 duration_ms");
    assert_eq!(
        invoker.host_inflight("plugin:tool_ok"),
        0,
        "调用结束后 in-flight 必须归零"
    );
}

#[tokio::test]
async fn invoke_tool_sidecar_failure_envelope_emits_on_error() {
    // tools/call 返回 {success:false, error} 失败信封 → Ok 携带 failure（不改
    // 错误处理语义），且中央返回处旁路广播 OnError（tool 形态业务失败也计数，
    // code=None → ctx 无 error_code 键）。
    let url = spawn_mock_http_mcp(
        r#"{"success":false,"error":"boom"}"#.to_string(),
        Duration::from_millis(0),
    )
    .await;
    let loader = Arc::new(MockLoader::new());
    let mut m = make_http_manifest("tool_fail_emit", &url);
    m.capabilities.tools = vec![p12_tool_cap("biz", None)];
    loader.add_manifest(m);
    let invoker = PluginInvokerImpl::new(loader);
    let bus = Arc::new(HookEventBus::new(32));
    invoker.set_hook_bus(bus.clone());
    let mut rx = bus.subscribe();

    let r = invoker
        .invoke_tool("tool_fail_emit", "biz", &json!({}))
        .await
        .expect("失败信封走 Ok 通道，不转 Err");
    assert!(!r.success);
    assert_eq!(r.error.as_deref(), Some("boom"));

    let ev_load = tokio::time::timeout(std::time::Duration::from_millis(500), rx.recv())
        .await
        .expect("应收到 OnLoad 事件")
        .expect("总线未关闭");
    assert_eq!(
        ev_load.hook,
        LifecycleHook::OnLoad,
        "sidecar spawn 握手完成的旁路 OnLoad 广播先于调用"
    );
    let ev = tokio::time::timeout(std::time::Duration::from_millis(500), rx.recv())
        .await
        .expect("应收到 OnError 事件")
        .expect("总线未关闭");
    assert_eq!(ev.hook, LifecycleHook::OnError);
    assert_eq!(ev.ctx.get("plugin_id"), Some(&json!("tool_fail_emit")));
    assert_eq!(ev.ctx.get("error"), Some(&json!("boom")));
    assert!(
        ev.ctx.get("error_code").is_none(),
        "tool 形态 success=false 无错误码，不得伪造"
    );
}

#[tokio::test]
async fn invoke_pipeline_plugin_sidecar_success_parses_plugin_result() {
    // tools/call（call_tool_raw_fragments）返回合法 PluginResult JSON →
    // attempt_sidecar_pipeline Ok 主路径端到端：state_updates 原样返回。
    let url = spawn_mock_http_mcp(
        r#"{"state_updates":{"approved":true},"skip_remaining":false}"#.to_string(),
        Duration::from_millis(0),
    )
    .await;
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_mock_http_pipeline_manifest("pipe_ok", &url));
    let invoker = PluginInvokerImpl::new(loader);
    let state = json!({"pipeline_id": "p-1"});
    let ctx = PluginContext::new(
        &state,
        json!({}),
        TenantContext::new("t1", "s1"),
        Uuid::new_v4(),
        agentos_core::types::ContentLoader::new(
            Arc::new(MockStorage),
            "run1".to_string(),
            "main".to_string(),
        ),
    );

    let pr = invoker
        .invoke_pipeline_plugin("pipe_ok", &ctx)
        .await
        .expect("合法 PluginResult 必须成功解析");
    assert_eq!(pr.state_updates.get("approved"), Some(&json!(true)));
    assert!(!pr.skip_remaining);
    assert!(pr.error.is_none());
    assert_eq!(
        invoker.host_inflight("plugin:pipe_ok"),
        0,
        "管道调用结束后 in-flight 必须归零"
    );
}

#[tokio::test]
async fn invoke_pipeline_plugin_unparseable_payload_is_parse_error() {
    // content[0].text 是合法 JSON 但不是 PluginResult 形状（数组）→
    // PARSE_ERROR（结果序列化错误分支），且非死亡类失败不触发重试。
    let url = spawn_mock_http_mcp("[1,2,3]".to_string(), Duration::from_millis(0)).await;
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_mock_http_pipeline_manifest("pipe_bad_parse", &url));
    let invoker = PluginInvokerImpl::new(loader);
    let state = json!({});
    let ctx = PluginContext::new(
        &state,
        json!({}),
        TenantContext::new("t1", "s1"),
        Uuid::new_v4(),
        agentos_core::types::ContentLoader::new(
            Arc::new(MockStorage),
            "run1".to_string(),
            "main".to_string(),
        ),
    );

    let err = invoker
        .invoke_pipeline_plugin("pipe_bad_parse", &ctx)
        .await
        .expect_err("非 PluginResult 形状必须报 PARSE_ERROR");
    assert_eq!(err.code.as_deref(), Some("PARSE_ERROR"), "got: {err:?}");
}

#[tokio::test]
async fn invoke_tool_capability_timeout_enforced_end_to_end() {
    // P12 端到端：工具声明 timeout_ms=300，tools/call 响应延迟 5s →
    // invoke_tool 返回结构化 CAPABILITY_TIMEOUT（message 含插件/工具/时长），
    // 超时放弃内层 future 后 in-flight 计数随 drop 回落。
    let url = spawn_mock_http_mcp(r#"{"ok":true}"#.to_string(), Duration::from_secs(5)).await;
    let loader = Arc::new(MockLoader::new());
    let mut m = make_http_manifest("slow_tool", &url);
    m.capabilities.tools = vec![p12_tool_cap("slow", Some(300))];
    loader.add_manifest(m);
    let invoker = PluginInvokerImpl::new(loader);

    let err = invoker
        .invoke_tool("slow_tool", "slow", &json!({}))
        .await
        .expect_err("到点必须放弃内层调用");
    assert_eq!(err.code.as_deref(), Some("CAPABILITY_TIMEOUT"));
    assert!(
        err.message.contains("slow_tool"),
        "message 须含插件 id: {err}"
    );
    assert!(err.message.contains("slow"), "message 须含工具名: {err}");
    assert!(err.message.contains("300ms"), "message 须含时长: {err}");
    assert_eq!(
        invoker.host_inflight("plugin:slow_tool"),
        0,
        "超时放弃内层 future 后 in-flight 必须回落"
    );
}

#[tokio::test]
async fn list_plugin_tools_reclaims_freshly_spawned_host_without_hooks() {
    // G2 主路径：本次新 spawn 的宿主（缓存原本无条目）校验完回收——kill +
    // 移除缓存，懒加载语义不被破坏。
    let url = spawn_mock_http_mcp(r#"{"ok":true}"#.to_string(), Duration::from_millis(0)).await;
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_http_manifest("g2_reclaim", &url));
    let invoker = PluginInvokerImpl::new(loader);

    let raw = invoker
        .list_plugin_tools("g2_reclaim")
        .await
        .expect("tools/list 应成功");
    assert_eq!(raw["ok"], true);
    assert!(
        invoker
            .mcp_clients
            .read()
            .get("plugin:g2_reclaim")
            .is_none(),
        "新 spawn 宿主校验后必须回收（缓存条目移除）"
    );
}

#[tokio::test]
async fn list_plugin_tools_keeps_freshly_spawned_host_with_lifecycle_hooks() {
    // G2 例外分支：声明 lifecycle_hooks 的插件 on_load 有副作用（起子进程/
    // 绑端口），探测完 kill 会毁掉初始化成果——此类新 spawn 宿主不回收，交
    // idle GC 空闲回收。
    let url = spawn_mock_http_mcp(r#"{"ok":true}"#.to_string(), Duration::from_millis(0)).await;
    let loader = Arc::new(MockLoader::new());
    let mut m = make_http_manifest("g2_keep_hooks", &url);
    m.capabilities.lifecycle_hooks = vec![LifecycleHook::OnLoad];
    loader.add_manifest(m);
    let invoker = PluginInvokerImpl::new(loader);

    let raw = invoker
        .list_plugin_tools("g2_keep_hooks")
        .await
        .expect("tools/list 应成功");
    assert_eq!(raw["ok"], true);
    assert!(
        invoker
            .mcp_clients
            .read()
            .get("plugin:g2_keep_hooks")
            .is_some(),
        "声明 lifecycle_hooks 的新 spawn 宿主不得被回收"
    );
}

#[tokio::test]
async fn list_plugin_tools_non_sidecar_errors() {
    // 仅 sidecar 支持 G2 describe；native（InProcess）直接报不支持（api 层
    // 据此跳过校验）。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_inprocess_manifest("g2_native"));
    let invoker = PluginInvokerImpl::new(loader);

    let err = invoker
        .list_plugin_tools("g2_native")
        .await
        .expect_err("非 sidecar 必须报不支持");
    assert!(err.message.contains("InProcess"), "got: {}", err.message);
}

#[tokio::test]
async fn set_router_invalidates_cached_sidecars_and_kills_processes() {
    // set_router 契约：缓存客户端的 initialize capabilities 是旧 router 快照
    // 且永不重连——注入 router 时必须全量废弃（kill + drain 缓存 + 清 spawn
    // 成员集快照），下次调用 respawn 拿新 capabilities。
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    let live = spawn_long_lived_stdio_client().await;
    assert!(live.is_alive().await, "前置：长驻假 sidecar 必须存活");
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert("plugin:stale_router".to_string(), Arc::clone(&live_arc));
    invoker.spawned_members.write().insert(
        "plugin:stale_router".to_string(),
        vec!["stale_router".to_string()],
    );

    invoker.set_router(Arc::new(RecordRouter {
        calls: Arc::new(std::sync::Mutex::new(Vec::new())),
    }));

    assert!(
        invoker.mcp_clients.read().is_empty(),
        "set_router 后缓存必须全量 drain"
    );
    assert!(
        invoker.spawned_members.read().is_empty(),
        "spawn 成员集快照与缓存条目同生命周期，必须一并清空"
    );
    // tokio runtime 内：异步 kill 任务已 spawn，进程最终被杀
    wait_until_killed(&live_arc, "被废弃的 sidecar 进程必须被 kill").await;
}

#[tokio::test]
async fn send_lifecycle_hook_delivery_failures_still_ok() {
    // fire-and-forget 语义：钩子投递失败（sidecar 客户端创建失败 / native 无
    // 通知通道）只留痕不阻断——send_lifecycle_hook 恒 Ok。
    let loader = Arc::new(MockLoader::new());
    // 非 python 裸命令 → 不走 venv fail-closed，connect 时 spawn 失败
    loader.add_manifest(make_sidecar_manifest(
        "hook_spawn_fail",
        "definitely_missing_command_98765",
    ));
    loader.add_manifest(make_inprocess_manifest("hook_native"));
    let invoker = PluginInvokerImpl::new(loader);
    let ctx = HookContext::new();

    let sidecar = invoker
        .send_lifecycle_hook("hook_spawn_fail", LifecycleHook::OnLoad, &ctx)
        .await;
    assert!(
        sidecar.is_ok(),
        "sidecar 客户端创建失败仅 warn: {sidecar:?}"
    );

    let native = invoker
        .send_lifecycle_hook("hook_native", LifecycleHook::OnUnload, &ctx)
        .await;
    assert!(native.is_ok(), "native 无通知通道 no-op Ok: {native:?}");
}

#[tokio::test]
async fn invoke_pipeline_native_missing_plugin_dir_errors() {
    // native pipeline 路径第二道校验：manifest.native.artifact 在但 loader 查
    // 不到插件目录 → NATIVE_PLUGIN_DIR_NOT_FOUND（未发现/已卸载）。
    let loader = Arc::new(MockLoader::new());
    let mut m = make_inprocess_manifest("native_pipe_no_dir");
    m.native = Some(agentos_core::traits::NativeArtifact {
        artifact: "whatever.dll".to_string(),
    });
    loader.add_manifest(m);
    let invoker =
        PluginInvokerImpl::new(loader).set_native_loader(Arc::new(NativePluginLoader::new()));
    let state = json!({});
    let ctx = PluginContext::new(
        &state,
        json!({}),
        TenantContext::new("t1", "s1"),
        Uuid::new_v4(),
        agentos_core::types::ContentLoader::new(
            Arc::new(MockStorage),
            "run1".to_string(),
            "main".to_string(),
        ),
    );

    let err = invoker
        .invoke_pipeline_plugin("native_pipe_no_dir", &ctx)
        .await
        .expect_err("查不到插件目录必须报错");
    assert_eq!(err.code.as_deref(), Some("NATIVE_PLUGIN_DIR_NOT_FOUND"));
}

#[test]
fn notify_if_crash_matches_only_panic_and_fatal_codes() {
    // 崩溃回调语义：Err 且错误码含 PANICKED/FATAL 才通知；其他错误码、无码、
    // Ok 一律不通知（native pipeline/tool 共用的中央判定）。
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    let fired = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
    let fired_clone = Arc::clone(&fired);
    invoker.on_crash(Arc::new(move |plugin_id: &str| {
        fired_clone.lock().unwrap().push(plugin_id.to_string());
    }));
    let pe = |code: Option<&str>| PluginError {
        message: "x".to_string(),
        code: code.map(str::to_string),
        source: None,
    };

    invoker.notify_if_crash::<PluginResult>("p_panic", &Err(pe(Some("NATIVE_EXECUTE_PANICKED"))));
    invoker.notify_if_crash::<PluginResult>("p_fatal", &Err(pe(Some("EXECUTE_FATAL"))));
    invoker.notify_if_crash::<PluginResult>("p_load", &Err(pe(Some("NATIVE_LOAD_FAILED"))));
    invoker.notify_if_crash::<PluginResult>("p_nocode", &Err(pe(None)));
    invoker.notify_if_crash::<PluginResult>("p_ok", &Ok(PluginResult::default()));

    assert_eq!(
        *fired.lock().unwrap(),
        vec!["p_panic".to_string(), "p_fatal".to_string()],
        "只有 PANICKED/FATAL 触发崩溃回调"
    );
}

#[test]
fn normalize_mcp_tool_result_envelope_with_non_bool_success_is_parse_error() {
    // ②-a 真 ToolExecutionResult 信封（同时带 success + data）但 success 非
    // bool → from_value 失败 → PARSE_ERROR（与 ②-b 同判，禁止成功偏置）。
    let err = normalize_mcp_tool_result(
        json!({"success": "yes", "data": {"k": "v"}}),
        "drifting_tool",
    )
    .unwrap_err();
    assert_eq!(err.code.as_deref(), Some("PARSE_ERROR"));
    assert!(
        err.message
            .contains("failed to parse MCP response as ToolExecutionResult"),
        "报错应指明信封解析失败: {err}"
    );
}

#[test]
fn compute_plugin_fingerprint_filters_junk_and_includes_config_files() {
    // 指纹输入面：.log 扩展 / 隐藏文件 / 子目录（__pycache__）不进指纹；
    // manifest.config_files 声明的配置文件（可在插件目录外）进指纹——mtime
    // 变化触发变化；文件消失按缺失降级（"0:0"）也产生变化。
    let tmp = tempfile::tempdir().unwrap();
    std::fs::write(tmp.path().join("server.py"), b"print(1)").unwrap();
    let cfg = tmp.path().join("llm.yaml");
    std::fs::write(&cfg, b"model: gpt\n").unwrap();
    let mut manifest = make_sidecar_manifest("fp_junk", "python server.py");
    manifest.config_files = vec![agentos_core::traits::ConfigFileMapping {
        id: "llm".to_string(),
        settings: None,
        path: cfg.to_string_lossy().into_owned(),
        label: "llm".to_string(),
        target: None,
        fields: vec![],
    }];
    let fp_base = compute_plugin_fingerprint(tmp.path(), &manifest);

    // 运行时杂物：诊断日志、隐藏 yaml、子目录产物——都不得改变指纹
    std::fs::write(tmp.path().join("diag.log"), b"log").unwrap();
    std::fs::write(tmp.path().join(".secret.yaml"), b"k: v").unwrap();
    std::fs::create_dir_all(tmp.path().join("__pycache__")).unwrap();
    std::fs::write(
        tmp.path()
            .join("__pycache__")
            .join("server.cpython-312.pyc"),
        b"x",
    )
    .unwrap();
    assert_eq!(
        compute_plugin_fingerprint(tmp.path(), &manifest),
        fp_base,
        "杂物（.log/隐藏/子目录）不得进指纹，否则 sidecar 写临时文件会误触发 respawn"
    );

    // 仅配置文件内容变化（源码未动）→ 指纹必须变化（config_files 纳入）
    std::thread::sleep(std::time::Duration::from_secs_f64(1.1));
    std::fs::write(&cfg, b"model: gpt-5\n").unwrap();
    let fp_cfg_changed = compute_plugin_fingerprint(tmp.path(), &manifest);
    assert_ne!(fp_cfg_changed, fp_base, "config_files 声明的文件必须进指纹");

    // 配置文件消失 → 缺失降级（"0:0"）同样改变指纹
    std::fs::remove_file(&cfg).unwrap();
    assert_ne!(
        compute_plugin_fingerprint(tmp.path(), &manifest),
        fp_cfg_changed,
        "配置文件消失必须产生指纹变化（触发 respawn 暴露缺配置）"
    );
}

#[tokio::test]
async fn is_plugin_stale_flags_change_only_after_ttl_window() {
    // Pull 热加载四分支：首次写入指纹不算过期；TTL 内短路；TTL 过期 + 指纹
    // 未变 → false（刷新检测时刻）；TTL 过期 + 指纹变 → true 并更新缓存。
    let tmp = tempfile::tempdir().unwrap();
    std::fs::write(tmp.path().join("server.py"), b"print(1)").unwrap();
    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "stale_ttl_full".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    let manifest = make_sidecar_manifest("stale_ttl_full", "python server.py");
    let invoker = PluginInvokerImpl::new(loader);
    let backdate = |invoker: &PluginInvokerImpl| {
        let (fp, _) = *invoker
            .fingerprints
            .read()
            .get("stale_ttl_full")
            .expect("首次检测应写入指纹缓存");
        invoker.fingerprints.write().insert(
            "stale_ttl_full".to_string(),
            (fp, Instant::now() - Duration::from_secs(2)),
        );
    };

    assert!(
        !invoker.is_plugin_stale("stale_ttl_full", &manifest).await,
        "首次不算过期"
    );
    backdate(&invoker);
    assert!(
        !invoker.is_plugin_stale("stale_ttl_full", &manifest).await,
        "TTL 过期但指纹未变 → 未过期"
    );
    std::thread::sleep(std::time::Duration::from_secs_f64(1.1));
    std::fs::write(tmp.path().join("server.py"), b"print(2) # changed").unwrap();
    backdate(&invoker);
    assert!(
        invoker.is_plugin_stale("stale_ttl_full", &manifest).await,
        "TTL 过期 + 指纹变 → 已过期（触发 respawn）"
    );
    assert!(
        !invoker.is_plugin_stale("stale_ttl_full", &manifest).await,
        "过期检测刷新时刻后，TTL 内立即再查应短路为 false"
    );
}

#[tokio::test]
async fn is_host_stale_solo_detects_change_after_ttl() {
    // 宿主级过期检测（独占宿主 = 插件自身指纹）：首次写入不算过期；TTL 内
    // 短路；TTL 过期 + 代码变更 → true（fast path 据此 kill respawn）。
    let tmp = tempfile::tempdir().unwrap();
    std::fs::write(tmp.path().join("server.py"), b"print(1)").unwrap();
    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "solo_stale".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    let manifest = make_sidecar_manifest("solo_stale", "python server.py");
    let invoker = PluginInvokerImpl::new(loader);
    let host_key = solo_host_key("solo_stale");

    assert!(
        !invoker.is_host_stale(&host_key, &manifest).await,
        "首次不算过期"
    );
    assert!(
        !invoker.is_host_stale(&host_key, &manifest).await,
        "TTL 内短路（不 stat 文件系统）"
    );
    std::thread::sleep(std::time::Duration::from_secs_f64(1.1));
    std::fs::write(tmp.path().join("server.py"), b"print(2) # changed").unwrap();
    // 回写 2s 前绕过 TTL 门（不真实等待）
    let (fp, _) = *invoker.fingerprints.read().get(&host_key).unwrap();
    invoker.fingerprints.write().insert(
        host_key.clone(),
        (fp, Instant::now() - Duration::from_secs(2)),
    );
    assert!(
        invoker.is_host_stale(&host_key, &manifest).await,
        "TTL 过期 + 代码变更 → 宿主过期"
    );
}

#[tokio::test]
async fn light_host_missing_spawn_snapshot_reuses_cached_client() {
    // 漂移检测保守分支：light 宿主缓存条目无 spawn 成员集快照（只可能来自
    // 测试手工注入）→ 按**无漂移**处理，不误杀，fast path 直接复用缓存实例。
    let loader = Arc::new(MockLoader::new());
    let manifest = make_light_manifest("snap_missing", "python server.py");
    loader.add_manifest(manifest.clone());
    let invoker = PluginInvokerImpl::new(loader);

    let host_key = invoker.resolve_host_key(&manifest);
    assert_eq!(host_key, "group:light:1");
    let live = spawn_long_lived_stdio_client().await;
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));
    // 刻意不写 spawned_members 快照

    let got = invoker
        .get_or_create_mcp_client(&manifest)
        .await
        .expect("快照缺失按无漂移处理，应复用缓存实例");
    assert!(
        Arc::ptr_eq(&got, &live_arc),
        "快照缺失不得误杀（保守复用缓存实例）"
    );
    assert!(
        live_arc.read().await.is_alive().await,
        "复用路径不得 kill 存活宿主"
    );
}

#[tokio::test]
async fn collect_plugin_roots_parents_of_manifest_dirs_via_discover() {
    // discover_new_plugins 的 roots 收集：从 AGENTOS_PLUGINS_DIR 递归找含
    // plugin.json 的目录（含无 manifest 的中间目录下钻），取父目录去重作为
    // discover roots（支持 tools/<plugin>/ 嵌套布局）。
    let tmp = tempfile::tempdir().unwrap();
    let base = tmp.path();
    std::fs::create_dir_all(base.join("tools").join("demo")).unwrap();
    std::fs::write(base.join("tools").join("demo").join("plugin.json"), b"{}").unwrap();
    // 无 manifest 的中间目录下钻：helper/ 自身无 plugin.json，但 inner/ 有
    std::fs::create_dir_all(base.join("tools").join("helper").join("inner")).unwrap();
    std::fs::write(
        base.join("tools")
            .join("helper")
            .join("inner")
            .join("plugin.json"),
        b"{}",
    )
    .unwrap();
    std::fs::create_dir_all(base.join("other")).unwrap();
    std::fs::write(base.join("other").join("plugin.json"), b"{}").unwrap();

    let _env = EnvVarGuard::set("AGENTOS_PLUGINS_DIR", base.to_string_lossy().as_ref());
    // 用户插件根同样进 roots（collect_plugin_roots 双根）：钉到空临时目录，
    // 否则会读到宿主机真实用户空间里的已装插件，期望集合随环境漂移。
    let empty_user_plugins = tempfile::tempdir().unwrap();
    let _user_env = EnvVarGuard::set(
        agentos_core::user_space::USER_PLUGINS_DIR_ENV,
        empty_user_plugins.path().to_string_lossy().as_ref(),
    );
    let loader = Arc::new(MockLoader::new());
    let invoker: Arc<dyn PluginInvoker> = Arc::new(PluginInvokerImpl::new(loader.clone()));
    let found = invoker
        .discover_new_plugins()
        .await
        .expect("discover 应成功");
    assert!(found.is_empty(), "MockLoader 无 manifest，发现 0 个");

    let mut roots = loader
        .captured_roots
        .read()
        .last()
        .expect("roots 已捕获")
        .clone();
    roots.sort();
    // other/plugin.json 的插件目录是 base/other，其父目录 = base 本身
    let expected: Vec<String> = [
        base.to_path_buf(),
        base.join("tools"),
        base.join("tools").join("helper"),
    ]
    .iter()
    .map(|p| p.to_string_lossy().into_owned())
    .collect();
    assert_eq!(roots, expected, "roots = manifest 目录的父目录去重集合");
}

// ═══════════════════════════════════════════════════════════════════════════
// 分支补测第二批：NativeHostServices 反调桥 / double-check 复用门 / 真 stdio
// 替身钩子投递 / JSON-RPC 协议错误映射 / 业务失败 OnError / build_mcp_client
// 三形态 / 后台 GC / 卸载 OnUnload 广播 / stale 驱逐 respawn 端到端
// ═══════════════════════════════════════════════════════════════════════════

/// 构造 NativeHostServices（私有结构，测试同模块可直构）。
fn make_host_services(
    router: Arc<dyn CapabilityRouter>,
    plugin_id: &str,
    on_blocking_thread: bool,
) -> NativeHostServices {
    NativeHostServices {
        router,
        plugin_id: plugin_id.to_string(),
        on_blocking_thread,
        response_buf: std::cell::UnsafeCell::new(String::with_capacity(4096)),
        err_buf: std::cell::UnsafeCell::new(String::with_capacity(256)),
    }
}

/// 恒失败的 router（capability 调用错误传播路径用）。
struct ErrRouter;
#[async_trait]
impl CapabilityRouter for ErrRouter {
    async fn handle(&self, _c: &str, _m: &str, _p: Value) -> Result<Value, McpError> {
        Err(McpError::ConnectionFailed {
            message: "boom-capability-down".to_string(),
        })
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn native_host_services_capability_calls_cover_param_and_error_shapes() {
    // G6/G7 桥（worker 线程 block_in_place 形态）四种入参形状：
    // ① 对象 params → 注入 _plugin_id 后透传，router 结果序列化返回；
    // ② 非对象 params → 包 {"_plugin_id"}（native 侧现状：载荷不保留）；
    // ③ params 非法 JSON → Err（早失败回传根因，不静默降级）；
    // ④ router Err → Err（错误消息写 err_buf 借出）。
    use agentos_native_sdk::HostServices as _;

    // ① 对象 params 成功路径
    let calls = Arc::new(std::sync::Mutex::new(Vec::new()));
    let svc = make_host_services(
        Arc::new(RecordRouter {
            calls: calls.clone(),
        }),
        "native_a",
        false,
    );
    let out = svc
        .call_capability("metrics", "record", r#"{"name":"calls","value":1}"#)
        .expect("对象 params 应成功");
    assert_eq!(out, r#"{"ok":true}"#, "响应 = router 结果的 JSON 序列化");
    let got = calls.lock().unwrap().clone();
    assert_eq!(got.len(), 1);
    assert_eq!(got[0].0, "metrics");
    assert_eq!(got[0].1, "record");
    assert_eq!(got[0].2["_plugin_id"], "native_a", "信任锚点必须注入（G6）");
    assert_eq!(got[0].2["name"], "calls", "原参数保留");

    // ② 非对象 params → 包 {"_plugin_id"}（payload 不保留，现状契约）
    let calls2 = Arc::new(std::sync::Mutex::new(Vec::new()));
    let svc2 = make_host_services(
        Arc::new(RecordRouter {
            calls: calls2.clone(),
        }),
        "native_b",
        false,
    );
    let out2 = svc2
        .call_capability("c", "m", "42")
        .expect("非对象 params 不报错（包对象续传）");
    assert_eq!(out2, r#"{"ok":true}"#);
    let got2 = calls2.lock().unwrap().clone();
    assert_eq!(got2[0].2, json!({"_plugin_id": "native_b"}));

    // ③ 非法 JSON → Err
    let svc3 = make_host_services(
        Arc::new(RecordRouter {
            calls: Arc::new(std::sync::Mutex::new(Vec::new())),
        }),
        "native_c",
        false,
    );
    let e = svc3
        .call_capability("c", "m", "not-json{")
        .expect_err("非法 params JSON 必须早失败");
    assert!(e.contains("params JSON invalid"), "报错应含根因: {e}");

    // ④ router Err → Err 传播
    let svc4 = make_host_services(Arc::new(ErrRouter), "native_d", false);
    let e4 = svc4
        .call_capability("c", "m", "{}")
        .expect_err("router 失败必须传播");
    assert!(e4.contains("boom-capability-down"), "错误消息应透传: {e4}");
}

#[tokio::test(flavor = "multi_thread")]
async fn native_host_services_blocking_thread_bridge_delivers_calls() {
    // G7 blocking 线程形态（生产路径：execute 跑在 spawn_blocking 上）：
    // 直接 Handle::block_on 桥接 sync→async，_plugin_id 注入与结果序列化同链路。
    use agentos_native_sdk::HostServices as _;
    let calls = Arc::new(std::sync::Mutex::new(Vec::new()));
    let svc = make_host_services(
        Arc::new(RecordRouter {
            calls: calls.clone(),
        }),
        "native_blk",
        true,
    );
    let (out, got) = tokio::task::spawn_blocking(move || {
        let out = svc
            .call_capability("tool-executor", "invoke", r#"{"cmd":"echo"}"#)
            .map(str::to_string)
            .map_err(str::to_string);
        (out, calls.lock().unwrap().clone())
    })
    .await
    .expect("spawn_blocking 不 panic");
    out.expect("blocking 线程桥接应成功");
    assert_eq!(got[0].2["_plugin_id"], "native_blk");
    assert_eq!(got[0].2["cmd"], "echo", "原参数保留");
}

#[tokio::test]
async fn double_check_reuse_returns_live_client_and_evicts_dead() {
    // spawn 锁后的 double-check 复用门：命中且存活 → touch 后返回同一实例；
    // 判死 → kill + 驱逐 + None（继续 spawn 路径）。
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));

    let live: SharedMcpClient = Arc::new(tokio::sync::RwLock::new(McpClient::new_http(
        "http://127.0.0.1:9/mcp",
        HashMap::new(),
        None,
    )));
    invoker
        .mcp_clients
        .write()
        .insert("plugin:dc_live".to_string(), Arc::clone(&live));
    let got = invoker
        .reuse_fresh_host_locked("plugin:dc_live")
        .await
        .expect("存活缓存应复用");
    assert!(
        Arc::ptr_eq(&got, &live),
        "double-check 必须返回同一缓存实例"
    );
    assert!(invoker.mcp_clients.read().get("plugin:dc_live").is_some());

    // 无 child 的 stdio client = 判死 → kill + 驱逐 + None
    invoker
        .mcp_clients
        .write()
        .insert("plugin:dc_dead".to_string(), unconnected_stdio_client());
    assert!(
        invoker
            .reuse_fresh_host_locked("plugin:dc_dead")
            .await
            .is_none(),
        "double-check 判死必须驱逐并返回 None"
    );
    assert!(
        invoker.mcp_clients.read().get("plugin:dc_dead").is_none(),
        "死实例必须从缓存清除"
    );
}

#[tokio::test]
async fn lifecycle_hook_and_tool_call_roundtrip_over_live_stdio_sidecar() {
    // 真 stdio 替身（外部进程 = 可 mock 边界）：echo JSON-RPC 的 python 进程。
    // 覆盖 send_lifecycle_hook 的 sidecar 投递成功路径（get_or_create →
    // is_alive → 通知真实写出）与 attempt_sidecar_tool 的 stdio 通道端到端 +
    // 缓存连接复用。
    assert_python_available();
    let script = "import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        m = json.loads(line)
    except Exception:
        continue
    if isinstance(m, dict) and 'id' in m:
        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': {'ok': True}}) + '\\n')
        sys.stdout.flush()
";
    let loader = Arc::new(MockLoader::new());
    let mut manifest = make_sidecar_manifest("stdio_echo", "external");
    manifest.mcp = Some(McpConfig {
        transport: McpTransport::Stdio,
        endpoint: Some(McpEndpoint {
            command: Some(python_exe().to_string()),
            args: vec!["-u".to_string(), "-c".to_string(), script.to_string()],
            ..Default::default()
        }),
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: None,
    });
    loader.add_manifest(manifest.clone());
    let invoker = PluginInvokerImpl::new(loader);

    // ① 生命周期钩子：连接建立 + is_alive → 通知写出（Ok）
    invoker
        .send_lifecycle_hook(
            "stdio_echo",
            LifecycleHook::OnPipelineStart,
            &HookContext::new(),
        )
        .await
        .expect("存活 sidecar 的生命周期通知必须成功");
    let cached = invoker
        .mcp_clients
        .read()
        .get("plugin:stdio_echo")
        .cloned()
        .expect("钩子路径应已建立宿主缓存");

    // ② 工具调用复用同一 stdio 连接：result 无 content → 原样提取 → 纯业务
    //    数据包 success 信封
    let tr = invoker
        .invoke_tool("stdio_echo", "echo_tool", &json!({"x": 1}))
        .await
        .expect("stdio 工具调用应成功");
    assert!(tr.success, "got: {:?}", tr);
    assert_eq!(tr.data["ok"], true, "替身 result 原样进 data");

    let again = invoker
        .mcp_clients
        .read()
        .get("plugin:stdio_echo")
        .cloned()
        .expect("调用后缓存条目保留");
    assert!(Arc::ptr_eq(&cached, &again), "工具调用必须复用缓存连接");

    // 清理替身进程（python 循环随 stdin 关闭退出，显式 kill 不留孤儿）
    again.write().await.kill().await.expect("清理替身进程");
}

#[tokio::test]
async fn invoke_pipeline_business_error_emits_on_error_with_code() {
    // Ok 携带 error 字段（业务失败）→ 结果照常返回，且中央返回处广播 OnError
    // （pipeline 形态 code 透传——lifecycle.plugin_error_total 的计数面）。
    let text = serde_json::json!({
        "state_updates": {}, "skip_remaining": false,
        "error": {"message": "step blew up", "code": "STEP_CODE", "source": null}
    })
    .to_string();
    let url = spawn_mock_http_mcp(text, Duration::from_millis(0)).await;
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_mock_http_pipeline_manifest("pipe_biz_err", &url));
    let invoker = PluginInvokerImpl::new(loader);
    let bus = Arc::new(HookEventBus::new(32));
    invoker.set_hook_bus(bus.clone());
    let mut rx = bus.subscribe();

    let state = json!({"pipeline_id": "p-9"});
    let ctx = make_e2e_ctx(&state);
    let pr = invoker
        .invoke_pipeline_plugin("pipe_biz_err", &ctx)
        .await
        .expect("业务失败走 Ok 通道，不转 Err");
    assert_eq!(
        pr.error.as_ref().map(|e| e.message.as_str()),
        Some("step blew up")
    );

    // OnLoad（spawn 旁路广播）之后是 OnError
    let ev_load = tokio::time::timeout(std::time::Duration::from_millis(500), rx.recv())
        .await
        .expect("应收到 OnLoad 事件")
        .expect("总线未关闭");
    assert_eq!(ev_load.hook, LifecycleHook::OnLoad);
    let ev = tokio::time::timeout(std::time::Duration::from_millis(500), rx.recv())
        .await
        .expect("应收到 OnError 事件")
        .expect("总线未关闭");
    assert_eq!(ev.hook, LifecycleHook::OnError);
    assert_eq!(ev.ctx.get("plugin_id"), Some(&json!("pipe_biz_err")));
    assert_eq!(ev.ctx.get("error"), Some(&json!("step blew up")));
    assert_eq!(
        ev.ctx.get("error_code"),
        Some(&json!("STEP_CODE")),
        "pipeline 形态业务失败必须透传错误码"
    );
}

/// mock HTTP MCP server（协议错误变体）：tools/call 回 JSON-RPC error 帧，
/// 其余请求回 `{"ok":true}`——call_tool 失败分支（非连接失败）端到端用。
async fn spawn_mock_http_mcp_tools_call_error(code: i64, message: &str) -> String {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    fn subseq(hay: &[u8], needle: &[u8]) -> Option<usize> {
        hay.windows(needle.len()).position(|w| w == needle)
    }
    fn content_length(headers: &[u8]) -> usize {
        String::from_utf8_lossy(headers)
            .to_ascii_lowercase()
            .lines()
            .find_map(|l| l.strip_prefix("content-length:"))
            .and_then(|v| v.trim().parse().ok())
            .unwrap_or(0)
    }
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let error_payload = serde_json::json!({
        "jsonrpc": "2.0", "id": 1,
        "error": {"code": code, "message": message}
    })
    .to_string();
    let generic_payload = r#"{"jsonrpc":"2.0","id":1,"result":{"ok":true}}"#.to_string();
    tokio::spawn(async move {
        loop {
            let Ok((mut sock, _)) = listener.accept().await else {
                return;
            };
            let error_payload = error_payload.clone();
            let generic_payload = generic_payload.clone();
            tokio::spawn(async move {
                loop {
                    let mut data = Vec::new();
                    let mut tmp = [0u8; 4096];
                    loop {
                        match sock.read(&mut tmp).await {
                            Ok(0) | Err(_) => return,
                            Ok(n) => {
                                data.extend_from_slice(&tmp[..n]);
                                if let Some(hdr_end) = subseq(&data, b"\r\n\r\n") {
                                    let cl = content_length(&data[..hdr_end]);
                                    if data.len() >= hdr_end + 4 + cl {
                                        break;
                                    }
                                }
                            }
                        }
                    }
                    let payload = if subseq(&data, b"\"tools/call\"").is_some() {
                        error_payload.clone()
                    } else {
                        generic_payload.clone()
                    };
                    let out = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
                        payload.len(),
                        payload
                    );
                    if sock.write_all(out.as_bytes()).await.is_err() {
                        return;
                    }
                }
            });
        }
    });
    url
}

#[tokio::test]
async fn invoke_pipeline_sidecar_jsonrpc_error_maps_to_call_failed() {
    // tools/call 回 JSON-RPC error（进程存活，HTTP 不判死）→ MCP_CALL_FAILED
    // （协议/工具错误，非死亡类，透明恢复不重试）。
    let url = spawn_mock_http_mcp_tools_call_error(-32602, "tool not found").await;
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_mock_http_pipeline_manifest("pipe_rpc_err", &url));
    let invoker = PluginInvokerImpl::new(loader);
    let state = json!({});
    let ctx = make_e2e_ctx(&state);

    let err = invoker
        .invoke_pipeline_plugin("pipe_rpc_err", &ctx)
        .await
        .expect_err("协议错误必须转 Err");
    assert_eq!(err.code.as_deref(), Some("MCP_CALL_FAILED"), "got: {err:?}");
    assert!(
        err.message.contains("tool not found"),
        "底层协议错误信息应透传: {}",
        err.message
    );
}

#[tokio::test]
async fn invoke_tool_sidecar_jsonrpc_error_maps_to_tool_call_failed() {
    // call_tool 失败 + 存活复查（HTTP 恒活）→ MCP_TOOL_CALL_FAILED；连接
    // 保留在缓存（网络/协议错误不驱逐，下次调用复用）。
    let url = spawn_mock_http_mcp_tools_call_error(-32602, "tool not found").await;
    let loader = Arc::new(MockLoader::new());
    let mut m = make_http_manifest("tool_rpc_err", &url);
    m.capabilities.tools = vec![p12_tool_cap("biz", None)];
    loader.add_manifest(m);
    let invoker = PluginInvokerImpl::new(loader);

    let err = invoker
        .invoke_tool("tool_rpc_err", "biz", &json!({}))
        .await
        .expect_err("协议错误必须转 Err");
    assert_eq!(
        err.code.as_deref(),
        Some("MCP_TOOL_CALL_FAILED"),
        "got: {err:?}"
    );
    assert!(
        err.message.contains("tool not found"),
        "底层协议错误信息应透传: {}",
        err.message
    );
    assert!(
        invoker
            .mcp_clients
            .read()
            .get("plugin:tool_rpc_err")
            .is_some(),
        "协议错误非进程死亡：缓存连接必须保留供复用"
    );
}

#[tokio::test]
async fn build_mcp_client_covers_transport_shapes_and_rejects_empty_command() {
    // 传输三路分流构造面：① HTTP 远程；② 外部 stdio 命令（env 解析成功路径）；
    // ③ 项目自带 sidecar（router 注入 → 反向调用包装 + working_dir + LOG_LEVEL
    // 透传）；④ 空 command → MCP_CONFIG_INVALID fail-closed。
    let tmp = tempfile::tempdir().unwrap();
    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "shape_solo".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    let invoker = PluginInvokerImpl::new(loader);
    invoker.set_router(Arc::new(RecordRouter {
        calls: Arc::new(std::sync::Mutex::new(Vec::new())),
    }));
    let _log = EnvVarGuard::set("LOG_LEVEL", "DEBUG");

    // ① HTTP 远程
    let http = make_http_manifest("shape_http", "http://127.0.0.1:9/mcp");
    invoker
        .build_mcp_client("plugin:shape_http", &http)
        .expect("HTTP 形态应可构造");

    // ② 外部 stdio 命令（含 env 解析成功路径——无占位符原样透传）
    let mut ext = make_sidecar_manifest("shape_ext", "unused");
    let mut env = std::collections::HashMap::new();
    env.insert("PLAIN_VAR".to_string(), "value".to_string());
    ext.mcp = Some(McpConfig {
        transport: McpTransport::Stdio,
        endpoint: Some(McpEndpoint {
            command: Some(python_exe().to_string()),
            args: vec!["--version".to_string()],
            env,
            ..Default::default()
        }),
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: None,
    });
    invoker
        .build_mcp_client("plugin:shape_ext", &ext)
        .expect("外部 stdio 形态应可构造");

    // ③ 项目自带 sidecar（非 python entry 免 venv 门；目录来自 loader）
    let solo = make_sidecar_manifest("shape_solo", "node server.js");
    invoker
        .build_mcp_client("plugin:shape_solo", &solo)
        .expect("自带 sidecar 形态应可构造");

    // ④ 外部 stdio 命令为空串 → MCP_CONFIG_INVALID
    let mut empty = make_sidecar_manifest("shape_empty", "unused");
    empty.mcp = Some(McpConfig {
        transport: McpTransport::Stdio,
        endpoint: Some(McpEndpoint {
            command: Some(String::new()),
            ..Default::default()
        }),
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: None,
    });
    let err = match invoker.build_mcp_client("plugin:shape_empty", &empty) {
        Err(e) => e,
        Ok(_) => panic!("空 command 必须 fail-closed，不得构造成功"),
    };
    assert_eq!(err.code.as_deref(), Some("MCP_CONFIG_INVALID"));
    assert!(
        err.message.contains("缺 command"),
        "错误应点名缺 command: {}",
        err.message
    );
}

#[tokio::test(start_paused = true)]
async fn start_idle_gc_background_pass_reclaims_idle_host() {
    // 后台 GC 任务（30s 周期，fake clock 驱动）：空闲超期宿主被回收（last_used
    // 条目清除）。无缓存条目的宿主 → unload 无真实进程依赖，确定性收敛。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_p12_manifest("gc_bg", 300));
    let invoker = Arc::new(PluginInvokerImpl::new(loader));
    invoker.touch_last_used("plugin:gc_bg");
    backdate_idle(&invoker, "plugin:gc_bg", 400);
    invoker.start_idle_gc();

    for _ in 0..45 {
        if invoker.last_used.read().get("plugin:gc_bg").is_none() {
            break;
        }
        tokio::time::advance(Duration::from_secs(1)).await;
    }
    assert!(
        invoker.last_used.read().get("plugin:gc_bg").is_none(),
        "后台 GC 必须在周期到达后回收空闲超期宿主"
    );
}

/// 串行化「进程级环境变量突变 × 无生命周期 manifest 的阈值计算」用例：
/// env 突变对并行计算是全局可见的（llvm-cov 插桩拉宽观察窗口），此类用例
/// 与其观察受害者必须互斥执行。
static ENV_SENSITIVE_LOCK: parking_lot::Mutex<()> = parking_lot::Mutex::new(());

#[tokio::test]
async fn idle_timeout_secs_sync_env_var_overrides_default() {
    // 优先级链第 2 级：manifest 未声明时全局环境变量覆盖默认（无效值回退）。
    let _serial = ENV_SENSITIVE_LOCK.lock();
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_sidecar_manifest("env_idle", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);
    let _g = EnvVarGuard::set("AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS", "77");
    assert_eq!(invoker.idle_timeout_secs_sync("env_idle"), 77);
    std::env::set_var("AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS", "not-a-number");
    assert_eq!(
        invoker.idle_timeout_secs_sync("env_idle"),
        agentos_core::traits::default_idle_timeout(),
        "非数字回退默认"
    );
    std::env::set_var("AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS", "0");
    assert_eq!(
        invoker.idle_timeout_secs_sync("env_idle"),
        agentos_core::traits::default_idle_timeout(),
        "0 视为无效回退默认"
    );
}

#[tokio::test]
async fn get_or_create_respawns_stale_solo_host_kills_old_process() {
    // fast path stale 驱逐端到端：指纹缓存回拨 + 记错值 → TTL 过期且指纹必变
    // → kill 旧宿主 + 逐出缓存 → slow path respawn（命令不存在必败，证明走的是
    // spawn 路径而非复用）。
    let tmp = tempfile::tempdir().unwrap();
    std::fs::write(tmp.path().join("server.py"), b"print(1)").unwrap();
    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "stale_solo".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    // entry 非裸 python（不走 venv fail-closed），respawn 时 spawn 必败
    let manifest =
        make_sidecar_manifest("stale_solo", "definitely_missing_command_98765 server.py");
    loader.add_manifest(manifest.clone());
    let invoker = PluginInvokerImpl::new(loader);

    let host_key = solo_host_key("stale_solo");
    let live = spawn_long_lived_stdio_client().await;
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));
    invoker.fingerprints.write().insert(
        host_key.clone(),
        (12345u64, Instant::now() - Duration::from_secs(2)),
    );

    let result = invoker.get_or_create_mcp_client(&manifest).await;
    assert!(
        result.is_err(),
        "stale 驱逐后必须走 respawn（命令不存在必败），不得返回旧实例: {:?}",
        result.map(|_| ()).err()
    );
    wait_until_killed(&live_arc, "stale 驱逐必须 kill 旧宿主进程").await;
    assert!(
        invoker.mcp_clients.read().get(&host_key).is_none(),
        "旧宿主缓存条目必须逐出"
    );
}

#[tokio::test]
async fn unload_broadcasts_on_unload_events_to_bus() {
    // OnUnload 旁路广播两路径：① no-host（InProcess）force_unload 也广播；
    // ② 宿主卸载逐成员广播（合宿组连坐语义的观察面）。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_inprocess_manifest("u_native"));
    loader.add_manifest(make_light_manifest("u_ma", "python server.py"));
    loader.add_manifest(make_light_manifest("u_mb", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);
    let bus = Arc::new(HookEventBus::new(32));
    invoker.set_hook_bus(bus.clone());
    let mut rx = bus.subscribe();

    // ① no-host 路径
    invoker.force_unload_impl("u_native").await.unwrap();
    let ev = tokio::time::timeout(std::time::Duration::from_millis(500), rx.recv())
        .await
        .expect("no-host 路径也要广播 OnUnload")
        .expect("总线未关闭");
    assert_eq!(ev.hook, LifecycleHook::OnUnload);
    assert_eq!(ev.ctx.get("plugin_id"), Some(&json!("u_native")));

    // ② 宿主路径：light 组整组卸载 → 逐成员广播
    invoker.resolve_host_key(&make_light_manifest("u_ma", "python server.py"));
    invoker.resolve_host_key(&make_light_manifest("u_mb", "python server.py"));
    invoker.mcp_clients.write().insert(
        "group:light:1".to_string(),
        Arc::new(tokio::sync::RwLock::new(McpClient::new_http(
            "http://127.0.0.1:9/mcp",
            HashMap::new(),
            None,
        ))),
    );
    invoker.unload_host("group:light:1", true).await.unwrap();
    let mut unloaded = Vec::new();
    for _ in 0..2 {
        let ev = tokio::time::timeout(std::time::Duration::from_millis(500), rx.recv())
            .await
            .expect("宿主卸载必须逐成员广播")
            .expect("总线未关闭");
        assert_eq!(ev.hook, LifecycleHook::OnUnload);
        unloaded.push(
            ev.ctx
                .get("plugin_id")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_string(),
        );
    }
    unloaded.sort();
    assert_eq!(
        unloaded,
        vec!["u_ma".to_string(), "u_mb".to_string()],
        "宿主卸载必须逐成员广播 OnUnload"
    );
}

#[test]
fn existing_host_key_and_members_edge_variants() {
    // 反查与成员集边界：light 声明但外部 MCP → 独占键兜底；独占键成员 = 键内
    // 嵌 plugin_id；无前缀杂键 → 空成员集（不 panic）。
    let loader = Arc::new(MockLoader::new());
    let mut ext = make_light_manifest("ext_guard", "external");
    ext.mcp = Some(McpConfig {
        transport: McpTransport::StreamableHttp,
        endpoint: Some(McpEndpoint {
            url: Some("http://127.0.0.1:9/mcp".to_string()),
            ..Default::default()
        }),
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: None,
    });
    loader.add_manifest(ext);
    let invoker = PluginInvokerImpl::new(loader);
    assert_eq!(
        invoker.existing_host_key_for("ext_guard"),
        Some("plugin:ext_guard".to_string()),
        "light 声明但外部 MCP 不进合宿 → 独占键"
    );
    assert_eq!(
        invoker.host_members("plugin:ext_guard"),
        vec!["ext_guard".to_string()]
    );
    assert!(
        invoker.host_members("bogus-key").is_empty(),
        "无前缀键 → 空成员集"
    );
}

#[test]
fn resolve_fingerprint_is_zero_without_plugin_dir() {
    // 目录不可得（插件已移除）→ 指纹 0（调用方视为已变化触发 respawn 暴露）。
    let loader = Arc::new(MockLoader::new());
    let manifest = make_sidecar_manifest("ghost_fp", "python server.py");
    let invoker = PluginInvokerImpl::new(loader);
    assert_eq!(
        invoker.resolve_fingerprint(&manifest),
        0,
        "无目录时指纹必须为 0"
    );
}

#[tokio::test]
async fn send_lifecycle_hook_native_with_loader_load_failure_still_ok() {
    // InProcess + 已注入 native loader：send_hook_via_execute 里 load_native
    // 失败（manifest 缺 native 声明）→ Err 仅 warn，钩子调用恒 Ok（不阻断管道）。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_inprocess_manifest("hook_native_load"));
    let invoker =
        PluginInvokerImpl::new(loader).set_native_loader(Arc::new(NativePluginLoader::new()));
    let r = invoker
        .send_lifecycle_hook(
            "hook_native_load",
            LifecycleHook::OnLoad,
            &HookContext::new(),
        )
        .await;
    assert!(r.is_ok(), "native 钩子加载失败只 warn 不阻断: {r:?}");
}

// ═══════════════════════════════════════════════════════════════════════════
// 宿主键/组名纯函数契约（合宿进程模型 §4.5 的边界）：组名合法性、宿主键
// 编解码往返、宿主目录向上探测——这些是装箱判定与 GC 归组的前置，错误会
// 静默改变驱逐爆炸半径。
// ═══════════════════════════════════════════════════════════════════════════

/// 组名合法性边界：空串 / 超长 / 非法字符（冒号会破坏 host_key 结构）
/// 一律拒绝；字母数字与 `_`/`-` 组合放行；长度恰好在 32 字边界内。
#[test]
fn is_valid_group_name_boundaries() {
    assert!(is_valid_group_name("light"));
    assert!(is_valid_group_name("light_stable-2"));
    assert!(!is_valid_group_name(""), "空组名非法");
    assert!(!is_valid_group_name("a:b"), "冒号破坏 host_key 结构");
    assert!(
        !is_valid_group_name("组名"),
        "非 ASCII 非法（进 spawn 参数）"
    );
    assert!(is_valid_group_name(&"x".repeat(32)), "32 字恰在允许内");
    assert!(!is_valid_group_name(&"x".repeat(33)), "33 字超限");
}

/// 宿主键编解码往返：组键 → (组名, 槽位)，非组键与畸形槽位返回 None。
#[test]
fn group_host_key_roundtrip_and_parse_rejects_foreign_keys() {
    let key = group_host_key("light_stable", 7);
    assert_eq!(key, "group:light_stable:7");
    assert_eq!(parse_group_slot(&key), Some(("light_stable", 7)));

    assert_eq!(parse_group_slot("plugin:llm_core"), None, "独占键无组槽");
    assert_eq!(parse_group_slot("group:light"), None, "缺槽位段");
    assert_eq!(
        parse_group_slot("group:light:not-a-number"),
        None,
        "槽位非数字"
    );
    assert_eq!(solo_host_key("llm_core"), "plugin:llm_core");
}

/// 宿主目录向上探测：插件目录层级不固定（plugins/shared/<type>/<phase>/<name>），
/// 逐级向上找最近的含 `_host` 子目录的祖先；无则 None。
#[test]
fn find_group_host_dir_walks_up_to_nearest_ancestor() {
    let tmp = tempfile::tempdir().unwrap();
    // 深层成员目录：plugins/shared/tools/demo/member/
    let member = tmp.path().join("shared/tools/demo/member");
    std::fs::create_dir_all(&member).unwrap();
    // 宿主目录在 tools/ 层（非直接父目录）
    let host = tmp.path().join("shared/tools/_host");
    std::fs::create_dir_all(&host).unwrap();

    let found = find_group_host_dir(&member).expect("应向上找到 _host");
    assert_eq!(found, host, "取最近祖先的 _host 子目录");

    // 无 _host 的树 → None（保守独占）
    let orphan = tmp.path().join("orphan/member");
    std::fs::create_dir_all(&orphan).unwrap();
    assert_eq!(find_group_host_dir(&orphan), None);
}

/// 合宿成员判定矩阵：组名缺失/非法、host_type 非 Sidecar、外部 MCP
/// （StreamableHttp 带 url / stdio 带 command）各自落回"不参与合宿"。
#[test]
fn is_cohost_member_exclusion_matrix() {
    // 合法组名 + sidecar + 无外部 MCP → 准入
    let ok = make_light_manifest("coh_ok", "python server.py");
    assert!(is_cohost_member(&ok));

    // 组名缺失
    let mut no_group = make_sidecar_manifest("no_group", "python server.py");
    no_group.host_group = None;
    assert!(!is_cohost_member(&no_group));

    // host_type 非 Sidecar（InProcess 天生单进程）
    let mut inproc = make_light_manifest("inproc", "python server.py");
    inproc.host_type = HostType::InProcess;
    assert!(!is_cohost_member(&inproc), "InProcess 不进合宿组");

    // 外部 MCP（StreamableHttp 带 url）：进程归外部所有
    let mut ext_http = make_light_manifest("ext_http", "external");
    ext_http.mcp = Some(McpConfig {
        transport: McpTransport::StreamableHttp,
        endpoint: Some(McpEndpoint {
            url: Some("http://127.0.0.1:9/mcp".to_string()),
            ..Default::default()
        }),
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: None,
    });
    assert!(!is_cohost_member(&ext_http));

    // 外部 MCP（stdio 带 command）：第三方命令同样不进组
    let mut ext_stdio = make_light_manifest("ext_stdio", "external");
    ext_stdio.mcp = Some(McpConfig {
        transport: McpTransport::Stdio,
        endpoint: Some(McpEndpoint {
            command: Some("npx".to_string()),
            ..Default::default()
        }),
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: None,
    });
    assert!(!is_cohost_member(&ext_stdio));

    // 带 mcp 配置但 endpoint 为空：无外部进程声明 → 照常参与合宿
    let mut no_endpoint = make_light_manifest("no_endpoint", "python server.py");
    no_endpoint.mcp = Some(McpConfig {
        transport: McpTransport::StreamableHttp,
        endpoint: None,
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: None,
    });
    assert!(
        is_cohost_member(&no_endpoint),
        "无 endpoint 声明 = 无外部进程，仍参与合宿"
    );
}
