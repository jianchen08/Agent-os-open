// @feature: FP-0.2.一 插件协议 | @ci: rust-test
//! invoker 单元测试（原单体 tests.rs 按主题拆分，共享 mock 辅助留主壳）。

use super::*;

use agentos_core::traits::{
    ConfigFileMapping, EnvConfigField, LoadedPlugin, McpConfig, McpEndpoint, McpTransport,
    PluginLifecycle, PluginManifest, PluginStatus,
};
use agentos_core::types::TenantContext;

use serde_json::json;

use std::path::PathBuf;

use uuid::Uuid;

/// 串行化 cdylib 加载的 native e2e 测试。
/// 直接 trait 对象的 root module 加载用全局初始化，多线程并发加载不同 cdylib 会竞争，
/// 需串行（生产环境单调用串行，无此问题）。
static NATIVE_E2E_LOCK: parking_lot::Mutex<()> = parking_lot::Mutex::new(());

/// `AGENTOS_MCP_START_TIMEOUT_SECS` 是进程级全局，本组用例串行消费（值各不同，
/// 并行下互踩会假红）。锁必须护住整个用例体（生产注入面即 env，env 需在全程
/// await 中保持注入值）——async 用例跨 await 持锁是刻意行为，见各用例 allow。
static HOST_START_TIMEOUT_ENV_LOCK: parking_lot::Mutex<()> = parking_lot::Mutex::new(());

/// 捕获 inner.handle 执行时的活跃租户上下文（None = 无 scope）。
type SeenTenantCalls =
    std::sync::Arc<std::sync::Mutex<Vec<(Option<agentos_core::types::TenantContext>, Value)>>>;

/// 串行化「进程级环境变量突变 × 无生命周期 manifest 的阈值计算」用例：
/// env 突变对并行计算是全局可见的（llvm-cov 插桩拉宽观察窗口），此类用例
/// 与其观察受害者必须互斥执行。
static ENV_SENSITIVE_LOCK: parking_lot::Mutex<()> = parking_lot::Mutex::new(());

struct MockLoader {
    manifests: RwLock<HashMap<String, PluginManifest>>,
    loaded: RwLock<HashMap<String, LoadedPlugin>>,
    /// task_11 N7 测试用：plugin_id → 插件目录路径（get_plugin_dir 返回它）。
    plugin_dirs: RwLock<HashMap<String, String>>,
    /// discover 收到的 roots（collect_plugin_roots 行为观察用）。
    captured_roots: RwLock<Vec<Vec<String>>>,
    /// load_config 返回的全量配置树（config_files 注入用例；None = 默认空 {}）。
    full_config: RwLock<Option<serde_json::Value>>,
    /// 服务条目 → 提供方插件 id（service_provider_ids 用例注入面）。
    service_providers: RwLock<HashMap<String, Vec<String>>>,
}

impl MockLoader {
    fn new() -> Self {
        Self {
            manifests: RwLock::new(HashMap::new()),
            loaded: RwLock::new(HashMap::new()),
            plugin_dirs: RwLock::new(HashMap::new()),
            captured_roots: RwLock::new(Vec::new()),
            full_config: RwLock::new(None),
            service_providers: RwLock::new(HashMap::new()),
        }
    }

    fn add_manifest(&self, manifest: PluginManifest) {
        self.manifests.write().insert(manifest.id.clone(), manifest);
    }

    fn set_full_config(&self, full: serde_json::Value) {
        *self.full_config.write() = Some(full);
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
            message: format!("plugin not found: {plugin_id}"),
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

    fn service_provider_ids(&self, service: &str) -> Vec<String> {
        self.service_providers
            .read()
            .get(service)
            .cloned()
            .unwrap_or_default()
    }

    async fn load_config(&self) -> Result<serde_json::Value, PluginError> {
        Ok(self
            .full_config
            .read()
            .clone()
            .unwrap_or_else(|| serde_json::json!({})))
    }
}

fn make_sidecar_manifest(id: &str, entry: &str) -> PluginManifest {
    PluginManifest {
        restricted_capabilities: Vec::new(),
        force_include_tools: Vec::new(),
        state: None,
        id: id.to_string(),
        name: format!("Test {id}"),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::Tool,
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
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        enabled: None,
        activation: None,
        provides: None,
        visibility: Default::default(),
        persistent_fields: vec![],
        aux_venvs: Vec::new(),
        export_fields: vec![],
    }
}

fn make_light_manifest(id: &str, entry: &str) -> PluginManifest {
    let mut m = make_sidecar_manifest(id, entry);
    m.host_group = Some("light".to_string());
    m
}

fn make_inprocess_manifest(id: &str) -> PluginManifest {
    PluginManifest {
        restricted_capabilities: Vec::new(),
        force_include_tools: Vec::new(),
        state: None,
        id: id.to_string(),
        name: format!("Test {id}"),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::Pipeline,
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
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        enabled: None,
        activation: None,
        provides: None,
        visibility: Default::default(),
        persistent_fields: vec![],
        aux_venvs: Vec::new(),
        export_fields: vec![],
    }
}

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

#[allow(clippy::await_holding_lock)]
#[tokio::test(flavor = "multi_thread")]
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
        plugins_dir.join("tool_core/pipeline_tool_core_native.dll")
    } else if cfg!(target_os = "macos") {
        plugins_dir.join("tool_core/libpipeline_tool_core_native.dylib")
    } else {
        plugins_dir.join("tool_core/libpipeline_tool_core_native.so")
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

    // tool_core 原生插件（单调用契约：引擎 for-each 注入的 current_call 触发执行）。
    let ctx_tool_state = json!({
        "current_call": {"name": "bash_execute", "id": "call_test1", "args": {"command": "echo agentos-native-ok"}},
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
    // tool_core 必然回写 tool_results；raw_tool_calls 清源归引擎 consume，插件不写该键。
    assert!(
        pr.state_updates.contains_key("tool_results"),
        "tool_results missing: {:?}",
        pr.state_updates.keys().collect::<Vec<_>>()
    );
    assert!(!pr.state_updates.contains_key("raw_tool_calls"));
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
        "tool result message should carry output: {msgs:?}"
    );
}

#[allow(clippy::await_holding_lock)]
#[tokio::test(flavor = "multi_thread")]
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
    assert!(tr.success, "tool should succeed: {tr:?}");
    assert_eq!(tr.data["tool"], "echo_tool");
    assert_eq!(tr.data["echo_args"]["hello"], "world");
}

fn make_http_manifest(id: &str, url: &str) -> PluginManifest {
    let mut m = make_sidecar_manifest(id, "external");
    m.mcp = Some(McpConfig {
        allowed_roots: Vec::new(),
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

fn breaker_test_invoker(
    dir: &tempfile::TempDir,
    threshold: u32,
) -> (PluginInvokerImpl, PluginManifest) {
    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "loop_plugin".to_string(),
        dir.path().to_string_lossy().into_owned(),
    );
    let manifest = make_sidecar_manifest("loop_plugin", "python server.py");
    loader.add_manifest(manifest.clone());
    let invoker = PluginInvokerImpl::new(loader).with_spawn_breaker_threshold(threshold);
    (invoker, manifest)
}

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

fn abs_python_interpreter() -> Option<String> {
    std::process::Command::new(python_exe())
        .arg("-c")
        .arg("import sys; print(sys.executable)")
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .filter(|p| !p.is_empty() && !p.chars().any(|c| c.is_whitespace()))
}

async fn spawn_long_lived_stdio_client() -> McpClient {
    // 直接单进程（不经 cmd/sh 包一层）：kill 是单进程直杀，无孙进程树遍历。
    // 套件并行时（cargo test 全 crate 多线程 spawn/kill 风暴）树杀延迟会溢出
    // kill_sidecar_if_any 的 2s 超时窗口，使"进程必须被 kill"族断言随机红。
    #[cfg(windows)]
    let (cmd, args): (&str, Vec<&str>) = ("ping", vec!["-n", "60", "127.0.0.1"]);
    #[cfg(unix)]
    let (cmd, args): (&str, Vec<&str>) = ("sleep", vec!["60"]);
    let mut client = McpClient::new_stdio(cmd, args.iter().map(|s| s.to_string()).collect());
    client
        .connect()
        .await
        .expect("spawn 长驻假 sidecar 进程应成功");
    client
}

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

fn assert_python_available() {
    let ok = std::process::Command::new(python_exe())
        .arg("--version")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    assert!(ok, "python 不可用——假死评估用例依赖真实 python 替身进程");
}

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

fn make_pipeline_sidecar_manifest(id: &str, invoke_entry: Option<&str>) -> PluginManifest {
    PluginManifest {
        restricted_capabilities: Vec::new(),
        force_include_tools: Vec::new(),
        state: None,
        id: id.to_string(),
        name: format!("Test {id}"),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::Pipeline,
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
        requires_content: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        enabled: None,
        activation: None,
        provides: None,
        visibility: Default::default(),
        invoke_entry: invoke_entry.map(str::to_string),
        persistent_fields: vec![],
        aux_venvs: Vec::new(),
        export_fields: vec![],
    }
}

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
    async fn get_messages_by_pipeline(
        &self,
        _pipeline_id: &str,
        _opts: agentos_core::traits::MessageQueryOpts,
    ) -> Result<Vec<agentos_core::types::MessageRecord>, agentos_core::types::StorageError> {
        Ok(vec![])
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

fn fake_trampoline_venv(
    root: &std::path::Path,
    venv_rel: &str,
) -> (std::path::PathBuf, std::path::PathBuf) {
    let venv = root.join(venv_rel);
    let trampoline = venv.join("Scripts").join("python.exe");
    std::fs::create_dir_all(trampoline.parent().unwrap()).unwrap();
    std::fs::write(&trampoline, b"").unwrap();
    let base = root.join("base-cpython-3.12");
    std::fs::create_dir_all(&base).unwrap();
    std::fs::write(base.join("python.exe"), b"").unwrap();
    // 真实 uv pyvenv.cfg 格式：home 乱序 + 前缀相近干扰键 home-x86（不得误配）。
    std::fs::write(
        venv.join("pyvenv.cfg"),
        format!(
            "home-x86 = D:\\should\\not\\match\nimplementation = CPython\nversion_info = 3.12.12\ninclude-system-site-packages = false\nprompt = demo\nhome = {}\n",
            base.display()
        ),
    )
    .unwrap();
    (trampoline, base)
}

struct UserRootGuard {
    _inner: EnvVarGuard,
    _serial: parking_lot::MutexGuard<'static, ()>,
}

impl UserRootGuard {
    fn pin_to(path: &std::path::Path) -> Self {
        let _serial = ENV_SENSITIVE_LOCK.lock();
        Self {
            _inner: EnvVarGuard::set("AGENTOS_USER_ROOT", path.to_string_lossy().as_ref()),
            _serial,
        }
    }
}

fn fake_registered_venv(user_root: &std::path::Path, key: &str) -> PathBuf {
    let venv_dir = user_root.join("plugin-venvs").join(key);
    for interp in [
        venv_dir.join("Scripts").join("python.exe"),
        venv_dir.join("bin").join("python"),
    ] {
        std::fs::create_dir_all(interp.parent().unwrap()).unwrap();
        std::fs::write(&interp, b"").unwrap();
    }
    find_registered_venv_interpreter(&venv_dir).expect("双布局占位必命中其一")
}

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

struct TenantCaptureRouter {
    seen: SeenTenantCalls,
}

#[async_trait]
impl CapabilityRouter for TenantCaptureRouter {
    async fn handle(
        &self,
        _capability: &str,
        _method: &str,
        params: Value,
    ) -> Result<Value, agentos_mcp::McpError> {
        self.seen
            .lock()
            .unwrap()
            .push((agentos_tenant::current(), params));
        Ok(json!({"ok": true}))
    }
}

fn capture_router() -> (SeenTenantCalls, Arc<PluginScopedRouter>) {
    let seen = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = Arc::new(PluginScopedRouter {
        plugin_id: "task_evaluate".to_string(),
        inner: Arc::new(TenantCaptureRouter { seen: seen.clone() }),
    });
    (seen, router)
}

async fn setup_synced_host_with_stale_union_baseline(
) -> (Arc<PluginInvokerImpl>, String, SharedMcpClient) {
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_light_manifest("mem_a", "python a.py"));
    loader.add_manifest(make_light_manifest("mem_b", "python b.py"));
    loader.add_manifest(make_light_manifest("mem_c", "python c.py"));
    let invoker = Arc::new(PluginInvokerImpl::new(loader));
    let host_key = "group:light:1".to_string();

    invoker.assign_light_host_with("mem_a", "light", 6);
    invoker.assign_light_host_with("mem_b", "light", 6);
    let live = spawn_long_lived_stdio_client().await;
    assert!(live.is_alive().await, "前置：长驻假 sidecar 必须存活");
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));
    // c 惰性装箱 + sync 补齐成功：镜像与每成员指纹表都已含 c（每成员指纹
    // 全 0——MockLoader 无插件目录，resolve_fingerprint 恒 0，纯成员集维度）
    invoker.assign_light_host_with("mem_c", "light", 6);
    invoker.spawned_members.write().insert(
        host_key.clone(),
        vec![
            "mem_a".to_string(),
            "mem_b".to_string(),
            "mem_c".to_string(),
        ],
    );
    invoker.snapshot_member_fingerprints(&host_key);
    // 并集基线停留补齐前（{a,b}），检测时刻拨回 2s 前过 TTL 门
    let stale_union = {
        let fps: std::collections::BTreeMap<String, u64> = ["mem_a", "mem_b"]
            .iter()
            .map(|p| (p.to_string(), 0u64))
            .collect();
        union_fingerprint_of(&host_key, &fps)
    };
    invoker.fingerprints.write().insert(
        host_key.clone(),
        (stale_union, Instant::now() - Duration::from_secs(2)),
    );
    (invoker, host_key, live_arc)
}

fn unconnected_stdio_client() -> SharedMcpClient {
    // 未连接的 stdio client：无子进程 → 判死（stdio 存活期恒持有 child，
    // 缓存中出现无 child 的 stdio 条目 = 未连接/已 kill 的残留，不可用；
    // 真实缓存只会写入 connect 成功的 client，本构造只为覆盖判死路径）
    Arc::new(tokio::sync::RwLock::new(McpClient::new_stdio(
        "python",
        vec!["-c".to_string(), "pass".to_string()],
    )))
}

use agentos_core::traits::default_capability_timeout_ms;

fn make_p12_manifest(id: &str, idle_timeout_secs: u64) -> PluginManifest {
    let mut m = make_sidecar_manifest(id, "python server.py");
    m.lifecycle = Some(PluginLifecycle {
        idle_timeout_secs: Some(idle_timeout_secs),
    });
    m
}

fn backdate_idle(invoker: &PluginInvokerImpl, plugin_id: &str, secs: u64) {
    invoker.member_last_used.write().insert(
        plugin_id.to_string(),
        Instant::now() - Duration::from_secs(secs),
    );
}

fn backdate_inflight_since(invoker: &PluginInvokerImpl, host_key: &str, secs: u64) {
    if let Some(registry) = invoker.inflight_since.read().get(host_key).cloned() {
        let mut map = registry.lock();
        let past = Instant::now() - Duration::from_secs(secs);
        for entry in map.values_mut() {
            entry.0 = past;
        }
    }
}

fn uptime_supports_backdate(secs: u64) -> bool {
    Instant::now()
        .checked_sub(Duration::from_secs(secs))
        .is_some()
}

fn healthy_http_client() -> SharedMcpClient {
    Arc::new(tokio::sync::RwLock::new(McpClient::new_http(
        "http://127.0.0.1:9/mcp",
        HashMap::new(),
        None,
    )))
}

fn pool_solo_host(invoker: &PluginInvokerImpl, plugin_id: &str, requests: u64) -> String {
    let host_key = format!("plugin:{plugin_id}");
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), healthy_http_client());
    invoker
        .host_spawned_at
        .write()
        .insert(host_key.clone(), Instant::now());
    if requests > 0 {
        invoker
            .host_request_counts
            .write()
            .insert(host_key.clone(), requests);
    }
    host_key
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

fn p12_tool_cap(name: &str, timeout_ms: Option<u64>) -> agentos_core::traits::ToolCapability {
    agentos_core::traits::ToolCapability {
        name: name.to_string(),
        description: None,
        input_schema: None,
        output_schema: None,
        category: None,
        ui: None,
        render: None,
        isolation_capable: None,
        smoke: None,
        timeout_ms,
    }
}

fn make_p22_llm_shape_manifest(id: &str) -> PluginManifest {
    let mut m = make_sidecar_manifest(id, "python server.py");
    m.plugin_type = PluginType::System;
    m.mcp = Some(McpConfig {
        allowed_roots: Vec::new(),
        transport: McpTransport::Stdio,
        endpoint: None,
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: Some(3600),
    });
    m
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
                    let (_key, _guard, _member_guard) = invoker.enter_sidecar_inflight(&m);
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
        backdate_idle(&invoker, "llm_stream", 400 + cycle * 30);
        invoker.run_idle_gc_pass().await;
        assert!(
            invoker.member_last_used.read().contains_key("llm_stream"),
            "流进行中第 {} 个 GC 周期不得回收成员",
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
    backdate_idle(&invoker, "llm_stream", 400);
    invoker.run_idle_gc_pass().await;
    assert!(
        !invoker.member_last_used.read().contains_key("llm_stream"),
        "流结束后超阈值成员应被正常回收"
    );
}

async fn spawn_mock_http_mcp(tools_call_text: String, tools_call_delay: Duration) -> String {
    spawn_mock_http_mcp_with_capture(tools_call_text, tools_call_delay, None, false).await
}

/// isError=true 变体：SDK 错误出口形态（2026-11-18 管道 7fb38fe8 实证）。
async fn spawn_mock_http_mcp_is_error(
    tools_call_text: String,
    tools_call_delay: Duration,
) -> String {
    spawn_mock_http_mcp_with_capture(tools_call_text, tools_call_delay, None, true).await
}

async fn spawn_mock_http_mcp_with_capture(
    tools_call_text: String,
    tools_call_delay: Duration,
    capture: Option<Arc<tokio::sync::Mutex<Vec<Value>>>>,
    is_error: bool,
) -> String {
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
        "content": [{"type": "text", "text": tools_call_text}], "isError": is_error
    });
    let generic_payload = serde_json::json!({"tools": [], "resultType": "complete"});
    let init_payload = serde_json::json!({
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "serverInfo": {"name": "mock", "version": "1.0"}
    });
    tokio::spawn(async move {
        loop {
            let Ok((mut sock, _)) = listener.accept().await else {
                return;
            };
            let tools_payload = tools_payload.clone();
            let generic_payload = generic_payload.clone();
            let init_payload = init_payload.clone();
            let capture = capture.clone();
            tokio::spawn(async move {
                // 帧缓冲跨读保留：rmcp 并发发多请求，多帧可能挤进同一次 TCP
                // 读——必须按 Content-Length 逐帧切分（处理完的帧 drain 掉，
                // 残余留给下轮），不能每轮丢弃重来（否则首帧 body 混入后续
                // 帧字节，解析失败被当 notification 回 202，id 错配挂死握手）。
                let mut buf: Vec<u8> = Vec::new();
                let mut tmp = [0u8; 4096];
                loop {
                    // 确保缓冲里有完整 header 段
                    let hdr_end = loop {
                        if let Some(p) = subseq(&buf, b"\r\n\r\n") {
                            break p;
                        }
                        match sock.read(&mut tmp).await {
                            Ok(0) | Err(_) => return,
                            Ok(n) => buf.extend_from_slice(&tmp[..n]),
                        }
                    };
                    // 确保 body 完整（按 Content-Length）
                    let cl = content_length(&buf[..hdr_end]);
                    let frame_end = hdr_end + 4 + cl;
                    while buf.len() < frame_end {
                        match sock.read(&mut tmp).await {
                            Ok(0) | Err(_) => return,
                            Ok(n) => buf.extend_from_slice(&tmp[..n]),
                        }
                    }
                    // GET（服务器→客户端 SSE 流探测）→ 405：客户端按规范回退
                    // 纯 POST 形态（rmcp get_stream 对 405 视为不支持 SSE）。
                    if buf.starts_with(b"GET") {
                        let out = "HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n";
                        if sock.write_all(out.as_bytes()).await.is_err() {
                            return;
                        }
                        buf.drain(..frame_end);
                        continue;
                    }
                    let body: serde_json::Value =
                        serde_json::from_slice(&buf[hdr_end + 4..frame_end]).unwrap_or(Value::Null);
                    let method = body.get("method").and_then(Value::as_str).unwrap_or("");
                    eprintln!(
                        "[MOCK2] method={method} id={:?} frame_end={frame_end} buf_len={}",
                        body.get("id"),
                        buf.len()
                    );
                    // notification（无 id）→ 202 Accepted（规范形态，无响应体）
                    let is_notification = body.get("id").is_none();
                    let payload = if is_notification {
                        String::new()
                    } else {
                        let id = body.get("id").cloned().unwrap_or(Value::Null);
                        let result = match method {
                            // 规范握手：协议版本 + 能力 + serverInfo（id 回显）
                            "initialize" => init_payload.clone(),
                            "tools/call" => {
                                if let Some(sink) = &capture {
                                    sink.lock()
                                        .await
                                        .push(body.get("params").cloned().unwrap_or(Value::Null));
                                }
                                tokio::time::sleep(tools_call_delay).await;
                                tools_payload.clone()
                            }
                            _ => generic_payload.clone(),
                        };
                        serde_json::json!({"jsonrpc": "2.0", "id": id, "result": result})
                            .to_string()
                    };
                    let status = if is_notification {
                        "HTTP/1.1 202 Accepted"
                    } else {
                        "HTTP/1.1 200 OK"
                    };
                    let out = format!(
                        "{status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
                        payload.len(),
                        payload
                    );
                    if sock.write_all(out.as_bytes()).await.is_err() {
                        return;
                    }
                    // 已处理帧出队，残余字节（后续帧）留待下轮
                    buf.drain(..frame_end);
                }
            });
        }
    });
    url
}

fn make_mock_http_pipeline_manifest(id: &str, url: &str) -> PluginManifest {
    let mut m = make_http_manifest(id, url);
    m.plugin_type = PluginType::Pipeline;
    m.invoke_entry = Some(format!("{id}.execute"));
    m
}

fn llm_config_files() -> Vec<ConfigFileMapping> {
    vec![ConfigFileMapping {
        id: "llm".to_string(),
        path: String::new(),
        label: "LLM 配置".to_string(),
        target: None,
        settings: None,
        fields: vec![EnvConfigField {
            name: "chat".to_string(),
            label: "默认模型".to_string(),
            field_type: "text".to_string(),
            required: false,
            description: None,
            extra: Some(
                json!({"default": "minimax-m3"})
                    .as_object()
                    .unwrap()
                    .clone(),
            ),
        }],
    }]
}

fn make_host_services(
    router: Arc<dyn CapabilityRouter>,
    plugin_id: &str,
    on_blocking_thread: bool,
) -> NativeHostServices {
    make_host_services_with(
        router,
        plugin_id,
        on_blocking_thread,
        None,
        Duration::from_secs(5),
    )
}

fn make_host_services_with(
    router: Arc<dyn CapabilityRouter>,
    plugin_id: &str,
    on_blocking_thread: bool,
    timeout_override_ms: Option<u64>,
    slow_call_warn_after: Duration,
) -> NativeHostServices {
    NativeHostServices {
        router,
        plugin_id: plugin_id.to_string(),
        identity: None,
        on_blocking_thread,
        response_buf: std::cell::UnsafeCell::new(String::with_capacity(4096)),
        err_buf: std::cell::UnsafeCell::new(String::with_capacity(256)),
        loader: Arc::new(MockLoader::new()),
        bridge_timeout_override_ms: timeout_override_ms,
        slow_call_warn_after,
    }
}

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

struct HungRouter;
#[async_trait]
impl CapabilityRouter for HungRouter {
    async fn handle(&self, _c: &str, _m: &str, _p: Value) -> Result<Value, McpError> {
        std::future::pending::<Result<Value, McpError>>().await
    }
}

struct DelayedRouter {
    delay: Duration,
}

#[async_trait]
impl CapabilityRouter for DelayedRouter {
    async fn handle(&self, _c: &str, _m: &str, _p: Value) -> Result<Value, McpError> {
        tokio::time::sleep(self.delay).await;
        Ok(json!({"status": "slow-ok"}))
    }
}

fn captured_events() -> &'static Arc<std::sync::Mutex<Vec<String>>> {
    static BUF: std::sync::OnceLock<Arc<std::sync::Mutex<Vec<String>>>> =
        std::sync::OnceLock::new();
    BUF.get_or_init(|| {
        let buf = Arc::new(std::sync::Mutex::new(Vec::new()));
        let _ = tracing::subscriber::set_global_default(CaptureSubscriber {
            events: Arc::clone(&buf),
        });
        buf
    })
}

struct CaptureSubscriber {
    events: Arc<std::sync::Mutex<Vec<String>>>,
}

struct FieldFlattener(String);
impl tracing::field::Visit for FieldFlattener {
    fn record_debug(&mut self, field: &tracing::field::Field, value: &dyn std::fmt::Debug) {
        use std::fmt::Write as _;
        let _ = write!(self.0, "{}={:?} ", field.name(), value);
    }
}

impl tracing::Subscriber for CaptureSubscriber {
    fn register_callsite(
        &self,
        _metadata: &'static tracing::Metadata<'static>,
    ) -> tracing::subscriber::Interest {
        tracing::subscriber::Interest::always()
    }
    fn enabled(&self, _metadata: &tracing::Metadata<'_>) -> bool {
        true
    }
    fn new_span(&self, _span: &tracing::span::Attributes<'_>) -> tracing::Id {
        tracing::Id::from_u64(1)
    }
    fn record(&self, _span: &tracing::Id, _values: &tracing::span::Record<'_>) {}
    fn record_follows_from(&self, _span: &tracing::Id, _follows: &tracing::Id) {}
    fn event(&self, event: &tracing::Event<'_>) {
        let mut flat = FieldFlattener(format!("[{}] ", event.metadata().level()));
        event.record(&mut flat);
        self.events.lock().unwrap().push(flat.0);
    }
    fn enter(&self, _span: &tracing::Id) {}
    fn exit(&self, _span: &tracing::Id) {}
}

fn count_warn_events(marker: &str) -> usize {
    captured_events()
        .lock()
        .unwrap()
        .iter()
        .filter(|e| e.starts_with("[WARN]") && e.contains(marker))
        .count()
}

#[tokio::test(flavor = "multi_thread")]
async fn native_bridge_hung_capability_returns_bounded_structured_error() {
    // 有界路径：router 永挂 + 短超时注入（禁真等长延迟）→ 到点放弃内层
    // future，结构化错误回传（含 capability/method/等待时长）。
    use agentos_native_sdk::HostServices as _;
    // 先安装全局捕获（本测试也发射 warn，callsite interest 须在发射前注册）。
    let _ = captured_events();
    let svc = make_host_services_with(
        Arc::new(HungRouter),
        "native_hang",
        false,
        Some(100),
        Duration::from_millis(20),
    );
    let started = std::time::Instant::now();
    let err = svc
        .call_capability("tool-executor", "invoke", r#"{"tool_name":"hung_tool"}"#)
        .expect_err("挂死 capability 必须有界返回错误而非静默驻留");
    let elapsed = started.elapsed();
    assert!(
        elapsed < Duration::from_secs(10),
        "有界返回不应真等长延迟（实际 {elapsed:?}）"
    );
    assert!(
        err.contains("capability tool-executor.invoke"),
        "错误应含 capability/method: {err}"
    );
    assert!(err.contains("timed out"), "错误应表明等待界到点: {err}");
    // 等待时长口径：消息中的毫秒数 = 实际等待，≥ 注入界量级（≥100ms 量级）
    let waited_ms: u64 = err
        .rsplit("after ")
        .next()
        .and_then(|tail| tail.split("ms").next())
        .and_then(|num| num.parse().ok())
        .expect("错误应含机器可读的等待毫秒数");
    assert!(
        (100..=120_000).contains(&waited_ms),
        "等待毫秒数应落在注入界量级（{waited_ms}ms）"
    );

    // 界 ≤ 慢阈形态：第一段即吃满总界，无第二段（total<=first 早出分支）。
    // 生产对齐场景：被调工具声明 timeout_ms < 5s 时挂死——先慢告警再即到点。
    let svc2 = make_host_services_with(
        Arc::new(HungRouter),
        "native_hang2",
        false,
        Some(50),
        NATIVE_BRIDGE_SLOW_CALL_WARN_AFTER,
    );
    let err2 = svc2
        .call_capability("tool-executor", "invoke", r#"{"tool_name":"hung_tool2"}"#)
        .expect_err("界 ≤ 慢阈时同样必须有界返回");
    assert!(
        err2.contains("capability tool-executor.invoke") && err2.contains("timed out"),
        "结构化错误契约不变: {err2}"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn native_bridge_slow_call_emits_warn_once_then_transparent_result() {
    // 慢调用 warn 触发分支：延迟(500ms) ≫ 慢阈(20ms) ≪ 总界(30s)——
    // 恰好一次 warn，结果仍原样透传（有界不改变正常语义）。
    use agentos_native_sdk::HostServices as _;
    // 先安装全局捕获（callsite interest 按全局 default 注册一次，必须先装）。
    let _ = captured_events();
    // capability 串即本测试唯一 marker（全局捕获缓冲并行共享，按 marker 过滤）。
    let marker = "native_bridge_slow_probe_capability";
    let svc = make_host_services_with(
        Arc::new(DelayedRouter {
            delay: Duration::from_millis(500),
        }),
        "native_slow",
        false,
        Some(30_000),
        Duration::from_millis(20),
    );
    let out = svc
        .call_capability(marker, "invoke", r#"{"tool_name":"slow_tool"}"#)
        .map(str::to_string)
        .map_err(str::to_string)
        .expect("总界内应答，调用应成功");
    assert!(out.contains("slow-ok"), "结果应原样透传: {out}");
    assert_eq!(
        count_warn_events(marker),
        1,
        "慢调用应恰好发一次 warn（含 marker capability）"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn native_bridge_fast_call_emits_no_slow_warn_and_passes_through() {
    // 未触发分支：立即应答 + 生产慢阈（5s，远大于任何调度抖动）→ 零 warn，
    // 桥透明（结果原样）。兼作有界包装后的正常路径回归。
    use agentos_native_sdk::HostServices as _;
    // 先安装全局捕获（callsite interest 按全局 default 注册一次，必须先装）。
    let _ = captured_events();
    // capability 串即本测试唯一 marker（全局捕获缓冲并行共享，按 marker 过滤）。
    let marker = "native_bridge_fast_probe_capability";
    let calls = Arc::new(std::sync::Mutex::new(Vec::new()));
    let svc = make_host_services_with(
        Arc::new(RecordRouter {
            calls: calls.clone(),
        }),
        "native_fast",
        false,
        None,
        NATIVE_BRIDGE_SLOW_CALL_WARN_AFTER,
    );
    let out = svc
        .call_capability(marker, "invoke", r#"{"tool_name":"fast_tool"}"#)
        .map(str::to_string)
        .map_err(str::to_string)
        .expect("立即应答应成功");
    assert_eq!(out, r#"{"ok":true}"#, "router 立即应答时桥透明（结果原样）");
    assert_eq!(count_warn_events(marker), 0, "快调用不应发慢调用 warn");
    let got = calls.lock().unwrap().clone();
    assert_eq!(got.len(), 1, "调用应到达 router");
}

async fn spawn_mock_http_mcp_tools_call_error(code: i64, message: String) -> String {
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
    tokio::spawn(async move {
        loop {
            let Ok((mut sock, _)) = listener.accept().await else {
                return;
            };
            let message = message.clone();
            tokio::spawn(async move {
                // 帧缓冲跨读保留 + 规范握手（同 spawn_mock_http_mcp：rmcp 客户端
                // 并发发 initialize/initialized/tools/call，逐帧切分、id 回显、
                // notification 202、GET 流探测 405）。
                let mut buf: Vec<u8> = Vec::new();
                let mut tmp = [0u8; 4096];
                let init_payload = serde_json::json!({
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "serverInfo": {"name": "mock", "version": "1.0"}
                });
                loop {
                    let hdr_end = loop {
                        if let Some(p) = subseq(&buf, b"\r\n\r\n") {
                            break p;
                        }
                        match sock.read(&mut tmp).await {
                            Ok(0) | Err(_) => return,
                            Ok(n) => buf.extend_from_slice(&tmp[..n]),
                        }
                    };
                    let cl = content_length(&buf[..hdr_end]);
                    let frame_end = hdr_end + 4 + cl;
                    while buf.len() < frame_end {
                        match sock.read(&mut tmp).await {
                            Ok(0) | Err(_) => return,
                            Ok(n) => buf.extend_from_slice(&tmp[..n]),
                        }
                    }
                    let (status, payload) = if buf.starts_with(b"GET") {
                        ("HTTP/1.1 405 Method Not Allowed", String::new())
                    } else {
                        let body: serde_json::Value =
                            serde_json::from_slice(&buf[hdr_end + 4..frame_end])
                                .unwrap_or(Value::Null);
                        let method = body.get("method").and_then(Value::as_str).unwrap_or("");
                        let id = body.get("id").cloned().unwrap_or(Value::Null);
                        let is_notification = body.get("id").is_none();
                        if is_notification {
                            ("HTTP/1.1 202 Accepted", String::new())
                        } else if method == "initialize" {
                            (
                                "HTTP/1.1 200 OK",
                                serde_json::json!({
                                    "jsonrpc": "2.0", "id": id, "result": init_payload
                                })
                                .to_string(),
                            )
                        } else if method == "tools/call" {
                            (
                                "HTTP/1.1 200 OK",
                                serde_json::json!({
                                    "jsonrpc": "2.0", "id": id,
                                    "error": {"code": code, "message": message}
                                })
                                .to_string(),
                            )
                        } else {
                            (
                                "HTTP/1.1 200 OK",
                                serde_json::json!({
                                    "jsonrpc": "2.0", "id": id, "result": {"tools": []}
                                })
                                .to_string(),
                            )
                        }
                    };
                    let out = format!(
                        "{status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
                        payload.len(),
                        payload
                    );
                    if sock.write_all(out.as_bytes()).await.is_err() {
                        return;
                    }
                    buf.drain(..frame_end);
                }
            });
        }
    });
    url
}

#[tokio::test(start_paused = true)]
async fn start_idle_gc_background_pass_reclaims_idle_host() {
    // 后台 GC 任务（30s 周期，fake clock 驱动）：空闲超期宿主被回收（last_used
    // 条目清除）。无缓存条目的宿主 → unload 无真实进程依赖，确定性收敛。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_p12_manifest("gc_bg", 300));
    let invoker = Arc::new(PluginInvokerImpl::new(loader));
    invoker.touch_member_last_used("gc_bg");
    backdate_idle(&invoker, "gc_bg", 400);
    invoker.start_idle_gc();

    for _ in 0..45 {
        if invoker.member_last_used.read().get("gc_bg").is_none() {
            break;
        }
        tokio::time::advance(Duration::from_secs(1)).await;
    }
    assert!(
        invoker.member_last_used.read().get("gc_bg").is_none(),
        "后台 GC 必须在周期到达后回收空闲超期成员"
    );
}

fn manifest_with_config_file(id: &str, namespace: &str, path: &str) -> PluginManifest {
    let mut m = make_light_manifest(id, "python server.py");
    m.config_files = vec![ConfigFileMapping {
        id: namespace.to_string(),
        path: path.to_string(),
        label: "测试配置".to_string(),
        target: None,
        settings: None,
        fields: vec![],
    }];
    m
}

fn mcp_text_payload(mcp_result: &serde_json::Value) -> serde_json::Value {
    let text = mcp_result
        .get("content")
        .and_then(|c| c.as_array())
        .and_then(|arr| arr.first())
        .and_then(|item| item.get("text"))
        .and_then(|t| t.as_str())
        .unwrap_or("null");
    serde_json::from_str(text).unwrap_or(serde_json::Value::Null)
}

fn write_delayed_responder(dir: &std::path::Path, name: &str, delay_secs: f64) -> String {
    let script = format!(
        concat!(
            "import sys, json, time\n",
            "time.sleep({delay})\n",
            "line = sys.stdin.readline()\n",
            "req = json.loads(line)\n",
            "resp = {{'jsonrpc': '2.0', 'id': req['id'], 'result': {{'protocolVersion': '2024-11-05'}}}}\n",
            "sys.stdout.write(json.dumps(resp) + '\\n')\n",
            "sys.stdout.flush()\n",
            "sys.stdin.readline()\n",
            "time.sleep(30)\n",
        ),
        delay = delay_secs
    );
    std::fs::write(dir.join(name), script).expect("写替身脚本");
    format!(
        "{} {name}",
        abs_python_interpreter().expect("python 已验可用")
    )
}

struct PreDispatchRouter {
    calls: std::sync::Arc<std::sync::Mutex<Vec<serde_json::Value>>>,
    delay_ms: u64,
    fail: bool,
    /// 工具自身报失败（信封 success:false）——模拟 fs 位置闸等工具内拒绝。
    envelope_fail: bool,
}

#[async_trait]
impl CapabilityRouter for PreDispatchRouter {
    async fn handle(
        &self,
        capability: &str,
        method: &str,
        params: serde_json::Value,
    ) -> Result<serde_json::Value, agentos_mcp::McpError> {
        assert_eq!(capability, "tool-executor");
        assert_eq!(method, "invoke");
        self.calls.lock().unwrap().push(params);
        if self.delay_ms > 0 {
            tokio::time::sleep(Duration::from_millis(self.delay_ms)).await;
        }
        if self.fail {
            return Err(agentos_mcp::McpError::Protocol {
                message: "dispatch boom".to_string(),
            });
        }
        if self.envelope_fail {
            return Ok(json!({
                "success": false,
                "error": "无法锚定工作空间：相对路径且未注入 workspace 锚",
            }));
        }
        Ok(json!({"success": true, "data": {"output": "ok"}}))
    }
}

fn pre_dispatch_manifest() -> PluginManifest {
    let mut manifest = make_inprocess_manifest("pipeline_tool_core");
    manifest.native = Some(agentos_core::traits::NativeArtifact {
        artifact: "pipeline_tool_core_native".to_string(),
        pre_dispatch: Some(agentos_core::traits::PreDispatchSpec {
            capability: "tool-executor".to_string(),
            method: "invoke".to_string(),
        }),
    });
    manifest
}

struct MessageCapture {
    messages: std::sync::Arc<parking_lot::Mutex<Vec<String>>>,
}

struct MessageFieldOnly<'a>(&'a mut String);

impl tracing::field::Visit for MessageFieldOnly<'_> {
    fn record_str(&mut self, field: &tracing::field::Field, value: &str) {
        if field.name() == "message" {
            self.0.push_str(value);
        }
    }
    fn record_debug(&mut self, field: &tracing::field::Field, value: &dyn std::fmt::Debug) {
        if field.name() == "message" {
            use std::fmt::Write;
            let _ = write!(self.0, "{value:?}");
        }
    }
}

impl tracing::Subscriber for MessageCapture {
    fn enabled(&self, _meta: &tracing::Metadata<'_>) -> bool {
        true
    }
    fn new_span(&self, _attrs: &tracing::span::Attributes<'_>) -> tracing::span::Id {
        tracing::span::Id::from_u64(1)
    }
    fn record(&self, _span: &tracing::Id, _values: &tracing::span::Record<'_>) {}
    fn record_follows_from(&self, _span: &tracing::Id, _follows: &tracing::Id) {}
    fn event(&self, event: &tracing::Event<'_>) {
        let mut msg = String::new();
        event.record(&mut MessageFieldOnly(&mut msg));
        self.messages.lock().push(msg);
    }
    fn enter(&self, _span: &tracing::Id) {}
    fn exit(&self, _span: &tracing::Id) {}
}

const RESUME_PIPELINE_GUARD_TEXT: &str = "resume_pipeline 拒绝派发：调用链未携带用户身份（执行上下文无注入 user；反调面身份由内核 scope 注入，params.user_id 已退役）pipeline=p-1";

const SESSION_CREATE_GUARD_TEXT: &str =
    "session.create 拒绝：调用链未携带用户身份（执行上下文无注入 user；承载管道归属必须可判）";

/// 反调守卫契约替身：捕获派发时刻的活跃 scope，并按真守卫同款判据裁决
/// （scope user 非空放行；缺失 → 逐字返回 fail-fast 锚文案）。
struct ReverseCallGuardRouter {
    seen: SeenTenantCalls,
    guard_text: &'static str,
}

#[async_trait]
impl CapabilityRouter for ReverseCallGuardRouter {
    async fn handle(
        &self,
        _capability: &str,
        _method: &str,
        params: Value,
    ) -> Result<Value, agentos_mcp::McpError> {
        let scope = agentos_tenant::current();
        self.seen.lock().unwrap().push((scope.clone(), params));
        let user_present = scope
            .as_ref()
            .and_then(|t| t.user_id.as_deref())
            .is_some_and(|u| !u.is_empty());
        if user_present {
            Ok(json!({ "success": true, "dispatched": true }))
        } else {
            Err(agentos_mcp::McpError::Protocol {
                message: self.guard_text.to_string(),
            })
        }
    }
}

fn ctx_with_user(state: &serde_json::Value) -> PluginContext<'_> {
    let mut tenant = TenantContext::new("t1", "s1");
    tenant.user_id = Some("u-maint".to_string());
    PluginContext::new(
        state,
        json!({}),
        tenant,
        Uuid::new_v4(),
        agentos_core::types::ContentLoader::new(
            std::sync::Arc::new(MockStorage),
            "run1".to_string(),
            "main".to_string(),
        ),
    )
}

async fn run_pre_dispatch_with_guard(
    guard_text: &'static str,
    seen: &SeenTenantCalls,
    ctx: &PluginContext<'_>,
) -> Value {
    let invoker = PluginInvokerImpl::new(std::sync::Arc::new(MockLoader::new()));
    invoker.set_router(std::sync::Arc::new(ReverseCallGuardRouter {
        seen: seen.clone(),
        guard_text,
    }));
    invoker
        .pre_dispatch_native("pipeline_tool_core", &pre_dispatch_manifest(), ctx)
        .await
        .expect("pre-dispatch entry")
}

#[tokio::test(flavor = "multi_thread")]
async fn native_bridge_identity_scope_carries_inherited_user() {
    use agentos_native_sdk::HostServices as _;
    let seen: SeenTenantCalls = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let mut parent = TenantContext::new("t-bridge", "s-bridge");
    parent.user_id = Some("u-bridge".to_string());
    let services = NativeHostServices {
        router: Arc::new(ReverseCallGuardRouter {
            seen: seen.clone(),
            guard_text: RESUME_PIPELINE_GUARD_TEXT,
        }),
        plugin_id: "pipeline_tool_core".to_string(),
        identity: Some(TenantContext::inherit(&parent)),
        on_blocking_thread: false,
        response_buf: std::cell::UnsafeCell::new(String::with_capacity(4096)),
        err_buf: std::cell::UnsafeCell::new(String::with_capacity(256)),
        loader: Arc::new(MockLoader::new()),
        bridge_timeout_override_ms: None,
        slow_call_warn_after: NATIVE_BRIDGE_SLOW_CALL_WARN_AFTER,
    };
    let out = services
        .call_capability("pipeline-state", "get", r#"{"key":"task.id"}"#)
        .expect("user 在场 → 守卫放行");
    let v: Value = serde_json::from_str(out).expect("响应为 JSON 信封");
    assert_eq!(v["success"], true, "反调守卫必须放行: {v}");
    let got = seen.lock().unwrap().clone();
    assert_eq!(got.len(), 1);
    let (tenant, _params) = &got[0];
    let tenant = tenant.as_ref().expect("identity 在场 → 必须有 scope");
    assert_eq!(tenant.tenant_id, "t-bridge");
    assert_eq!(tenant.session_id, "s-bridge");
    assert_eq!(
        tenant.user_id.as_deref(),
        Some("u-bridge"),
        "user 必须随桥 scope 透传"
    );
}

mod cohost_members;
mod e2e_group;
mod e2e_invoke;
mod fingerprint_venv;
mod invocation;
mod light_pool_freeze;
mod router_packing;
mod unload_gc;
