//! PluginInvoker 实现
//!
//! 按 host_type 透明分发调用：
//! - InProcess: 直接调用 `dyn PipelinePlugin::execute`（零 IPC 开销）
//! - Sidecar: 通过 MCP 客户端走 JSON-RPC 协议调用（进程隔离）
//!
//! [来源: docs/tasks/task_05_plugin_system.md AC-04-5/AC-04-6]

use std::collections::HashMap;
use std::sync::Arc;

use async_trait::async_trait;
use lingxi_core::traits::{
    HookContext, HostType, LifecycleHook, PluginInvoker, PluginLoader, PluginManifest, PluginType,
};
use lingxi_core::types::{PluginContext, PluginError, PluginResult, ToolExecutionResult};
use lingxi_mcp::{McpClient, McpError};
use parking_lot::RwLock;
use tracing::{error, info, warn};

/// PluginInvoker 实现。
///
/// 管理插件实例和 MCP 客户端连接，按 host_type 透明分发调用。
/// 支持崩溃隔离：检测子进程崩溃后卸载能力 + 告警。
pub struct PluginInvokerImpl {
    /// 插件加载器（用于查找 manifest）
    loader: Arc<dyn PluginLoader>,
    /// 已连接的 MCP 客户端 {plugin_id: McpClient}
    mcp_clients: RwLock<HashMap<String, Arc<tokio::sync::Mutex<McpClient>>>>,
    /// 崩溃回调（插件崩溃时调用）
    #[allow(clippy::type_complexity)]
    crash_callbacks: RwLock<Vec<Arc<dyn Fn(&str) + Send + Sync>>>,
}

impl PluginInvokerImpl {
    /// 创建 PluginInvoker。
    pub fn new(loader: Arc<dyn PluginLoader>) -> Self {
        Self {
            loader,
            mcp_clients: RwLock::new(HashMap::new()),
            crash_callbacks: RwLock::new(Vec::new()),
        }
    }

    /// 注册崩溃回调。
    pub fn on_crash(&self, callback: Arc<dyn Fn(&str) + Send + Sync>) {
        self.crash_callbacks.write().push(callback);
    }

    /// 通知崩溃回调。
    fn notify_crash(&self, plugin_id: &str) {
        let callbacks = self.crash_callbacks.read();
        for cb in callbacks.iter() {
            cb(plugin_id);
        }
    }

    /// 获取或创建 MCP 客户端（按需加载）。
    async fn get_or_create_mcp_client(
        &self,
        manifest: &PluginManifest,
    ) -> Result<Arc<tokio::sync::Mutex<McpClient>>, PluginError> {
        // 检查缓存（不跨 await 持有读锁）
        let cached = {
            let clients = self.mcp_clients.read();
            clients.get(&manifest.id).cloned()
        };

        if let Some(client) = cached {
            // 检查进程是否存活（读锁已释放）
            let mut client_guard = client.lock().await;
            if client_guard.is_alive().await {
                return Ok(Arc::clone(&client));
            }
            // 进程已崩溃——显式 kill 旧客户端再创建新的
            error!("Plugin process crashed: {}", manifest.id);
            let _ = client_guard.kill().await;
            drop(client_guard);
            self.notify_crash(&manifest.id);
            // 从缓存中移除旧客户端
            self.mcp_clients.write().remove(&manifest.id);
        }

        // 创建新的 MCP 客户端
        let (command, args) = self.parse_entry(&manifest.entry)?;
        let mut client = McpClient::new_stdio(command, args);

        client.connect().await.map_err(|e| PluginError {
            message: format!("MCP connect failed: {}", e),
            code: Some("MCP_CONNECT_FAILED".to_string()),
            source: Some("plugin-invoker".to_string()),
        })?;

        // initialize 握手
        client.initialize().await.map_err(|e| PluginError {
            message: format!("MCP initialize failed: {}", e),
            code: Some("MCP_INIT_FAILED".to_string()),
            source: Some("plugin-invoker".to_string()),
        })?;

        info!(
            "MCP client connected and initialized: plugin={}",
            manifest.id
        );

        let client_arc = Arc::new(tokio::sync::Mutex::new(client));

        // 缓存
        {
            let mut clients = self.mcp_clients.write();
            clients.insert(manifest.id.clone(), Arc::clone(&client_arc));
        }

        Ok(client_arc)
    }

    /// 解析 entry 字段为 command + args。
    ///
    /// entry 格式：`python3 -m my_plugin` 或 `/usr/bin/python3 server.py`
    fn parse_entry(&self, entry: &str) -> Result<(String, Vec<String>), PluginError> {
        let parts: Vec<&str> = entry.split_whitespace().collect();
        if parts.is_empty() {
            return Err(PluginError {
                message: "empty entry".to_string(),
                code: Some("EMPTY_ENTRY".to_string()),
                source: Some("plugin-invoker".to_string()),
            });
        }
        let command = parts[0].to_string();
        let args = parts[1..].iter().map(|s| s.to_string()).collect();
        Ok((command, args))
    }

    /// 检查插件进程健康状态。
    pub async fn check_health(&self, plugin_id: &str) -> bool {
        let client_arc = {
            let clients = self.mcp_clients.read();
            clients.get(plugin_id).cloned()
        };
        if let Some(client) = client_arc {
            let guard = client.lock().await;
            guard.is_alive().await
        } else {
            false
        }
    }

    /// 强制卸载崩溃的插件。
    pub async fn force_unload(&self, plugin_id: &str) -> Result<(), PluginError> {
        let client_arc = {
            let mut clients = self.mcp_clients.write();
            clients.remove(plugin_id)
        };

        if let Some(client_arc) = client_arc {
            let mut client = client_arc.lock().await;
            if let Err(e) = client.kill().await {
                warn!("Failed to kill crashed plugin {}: {}", plugin_id, e);
            }
        }

        // 也通过 loader 卸载
        let _ = self.loader.unload(plugin_id).await;

        info!("Force unloaded plugin: {}", plugin_id);
        Ok(())
    }
}

#[async_trait]
impl PluginInvoker for PluginInvokerImpl {
    /// 调用管道插件执行。
    ///
    /// 按 manifest 的 host_type 透明分发：
    /// - InProcess: 直接调用 dyn PipelinePlugin::execute（当前仅返回未实现错误，实际 trait 对象注册在引擎层）
    /// - Sidecar: 通过 MCP 客户端走 JSON-RPC tools/call
    async fn invoke_pipeline_plugin(
        &self,
        plugin_id: &str,
        ctx: &PluginContext,
    ) -> Result<PluginResult, PluginError> {
        let loaded = self.loader.load(plugin_id).await?;
        let manifest = &loaded.manifest;

        match manifest.host_type {
            HostType::InProcess => {
                // InProcess 模式：实际 trait 对象注册在引擎层
                // PluginInvoker 仅做分发，不持有 dyn PipelinePlugin
                // 引擎层会直接调用 PipelinePlugin::execute
                Err(PluginError {
                    message: format!(
                        "InProcess plugin '{}' should be called directly by engine, not via MCP",
                        plugin_id
                    ),
                    code: Some("INPROCESS_DIRECT_CALL".to_string()),
                    source: Some("plugin-invoker".to_string()),
                })
            }
            HostType::Sidecar => {
                // Sidecar 模式：通过 MCP 客户端调用
                let client_arc = self.get_or_create_mcp_client(manifest).await?;
                let client = client_arc.lock().await;

                // 检查进程健康
                if !client.is_alive().await {
                    drop(client);
                    self.notify_crash(plugin_id);
                    return Err(PluginError {
                        message: format!("plugin process crashed: {}", plugin_id),
                        code: Some("PLUGIN_CRASHED".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    });
                }

                // 调用 tools/call
                let tool_args = serde_json::json!({
                    "state": ctx.state,
                    "config": ctx.config,
                });

                let result = client.call_tool("execute", &tool_args).await.map_err(|e| {
                    let is_crash = matches!(e, McpError::Transport { .. });
                    if is_crash {
                        drop(client);
                        self.notify_crash(plugin_id);
                    }
                    PluginError {
                        message: format!("MCP call failed: {}", e),
                        code: Some("MCP_CALL_FAILED".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    }
                })?;

                // 将 MCP 返回结果转为 PluginResult
                let plugin_result: PluginResult =
                    serde_json::from_value(result).unwrap_or(PluginResult {
                        state_updates: HashMap::new(),
                        route_signal: None,
                        skip_remaining: false,
                        error: Some(PluginError {
                            message: "failed to parse MCP response as PluginResult".to_string(),
                            code: Some("PARSE_ERROR".to_string()),
                            source: None,
                        }),
                    });

                Ok(plugin_result)
            }
        }
    }

    /// 调用工具插件执行。
    ///
    /// 按 host_type 透明分发：
    /// - InProcess: 直接调用（引擎层注册的工具函数）
    /// - Sidecar: 通过 MCP 客户端走 JSON-RPC tools/call
    async fn invoke_tool(
        &self,
        plugin_id: &str,
        tool_name: &str,
        inputs: &serde_json::Value,
    ) -> Result<ToolExecutionResult, PluginError> {
        let loaded = self.loader.load(plugin_id).await?;
        let manifest = &loaded.manifest;

        match manifest.host_type {
            HostType::InProcess => Err(PluginError {
                message: format!(
                    "InProcess tool '{}' should be called directly by engine",
                    tool_name
                ),
                code: Some("INPROCESS_DIRECT_CALL".to_string()),
                source: Some("plugin-invoker".to_string()),
            }),
            HostType::Sidecar => {
                let client_arc = self.get_or_create_mcp_client(manifest).await?;
                let client = client_arc.lock().await;

                if !client.is_alive().await {
                    drop(client);
                    self.notify_crash(plugin_id);
                    return Err(PluginError {
                        message: format!("plugin process crashed: {}", plugin_id),
                        code: Some("PLUGIN_CRASHED".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    });
                }

                let result =
                    client
                        .call_tool(tool_name, inputs)
                        .await
                        .map_err(|e| PluginError {
                            message: format!("MCP tool call failed: {}", e),
                            code: Some("MCP_TOOL_CALL_FAILED".to_string()),
                            source: Some("plugin-invoker".to_string()),
                        })?;

                // 将 MCP 返回结果转为 ToolExecutionResult
                let tool_result: ToolExecutionResult =
                    serde_json::from_value(result).map_err(|e| PluginError {
                        message: format!(
                            "failed to parse MCP response as ToolExecutionResult: {}",
                            e
                        ),
                        code: Some("PARSE_ERROR".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    })?;

                Ok(tool_result)
            }
        }
    }

    /// 发送生命周期钩子事件到指定插件。
    async fn send_lifecycle_hook(
        &self,
        plugin_id: &str,
        hook: LifecycleHook,
        context: &HookContext,
    ) -> Result<(), PluginError> {
        let loaded = self.loader.load(plugin_id).await?;
        let manifest = &loaded.manifest;

        // 组合插件不需要生命周期钩子（ADR ⑥）
        if manifest.plugin_type == PluginType::Composite {
            return Ok(());
        }

        // Sidecar 模式发送 MCP 通知
        if manifest.host_type == HostType::Sidecar {
            if let Ok(client_arc) = self.get_or_create_mcp_client(manifest).await {
                let client = client_arc.lock().await;

                if client.is_alive().await {
                    let hook_method = match hook {
                        LifecycleHook::OnLoad => "notifications/on_load",
                        LifecycleHook::OnUnload => "notifications/on_unload",
                        LifecycleHook::OnPipelineStart => "notifications/on_pipeline_start",
                        LifecycleHook::OnPipelineEnd => "notifications/on_pipeline_end",
                        LifecycleHook::OnError => "notifications/on_error",
                    };

                    // 使用 send_notification 发送通知（不等响应）
                    if let Err(e) = client
                        .send_notification(
                            hook_method,
                            Some(serde_json::to_value(context.tags()).unwrap_or_default()),
                        )
                        .await
                    {
                        warn!("Lifecycle notification failed for {}: {}", plugin_id, e);
                    }
                }
            }
        }

        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use lingxi_core::traits::{LoadedPlugin, PluginManifest, PluginStatus};
    use lingxi_core::types::TenantContext;
    use serde_json::json;
    use uuid::Uuid;

    /// Mock PluginLoader for testing
    struct MockLoader {
        manifests: RwLock<HashMap<String, PluginManifest>>,
        loaded: RwLock<HashMap<String, LoadedPlugin>>,
    }

    impl MockLoader {
        fn new() -> Self {
            Self {
                manifests: RwLock::new(HashMap::new()),
                loaded: RwLock::new(HashMap::new()),
            }
        }

        fn add_manifest(&self, manifest: PluginManifest) {
            self.manifests.write().insert(manifest.id.clone(), manifest);
        }
    }

    #[async_trait]
    impl PluginLoader for MockLoader {
        async fn discover(&self, _root_paths: &[&str]) -> Result<Vec<PluginManifest>, PluginError> {
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

        fn get_status(&self, plugin_id: &str) -> PluginStatus {
            self.loaded
                .read()
                .get(plugin_id)
                .map(|p| p.status.clone())
                .unwrap_or(PluginStatus::Discovered)
        }
    }

    #[allow(dead_code)]
    fn make_sidecar_manifest(id: &str, entry: &str) -> PluginManifest {
        PluginManifest {
            id: id.to_string(),
            name: format!("Test {}", id),
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Tool,
            pipeline_role: None,
            language: "python".to_string(),
            host_type: HostType::Sidecar,
            entry: entry.to_string(),
            capabilities: Default::default(),
            dependencies: vec![],
            permissions: Default::default(),
            error_policy: Default::default(),
            priority: 100,
            mcp: None,
            requires_content: None,
        }
    }

    fn make_inprocess_manifest(id: &str) -> PluginManifest {
        PluginManifest {
            id: id.to_string(),
            name: format!("Test {}", id),
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Pipeline,
            pipeline_role: None,
            language: "rust".to_string(),
            host_type: HostType::InProcess,
            entry: "test_entry".to_string(),
            capabilities: Default::default(),
            dependencies: vec![],
            permissions: Default::default(),
            error_policy: Default::default(),
            priority: 100,
            mcp: None,
            requires_content: None,
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
    async fn test_invoke_inprocess_returns_error() {
        let loader = Arc::new(MockLoader::new());
        loader.add_manifest(make_inprocess_manifest("rust_plugin"));

        let invoker = PluginInvokerImpl::new(loader);
        let ctx = PluginContext::new(
            json!({}),
            json!({}),
            TenantContext::new("t1", "s1"),
            Uuid::new_v4(),
            lingxi_core::types::ContentLoader::new(
                std::sync::Arc::new(MockStorage),
                "run1".to_string(),
                "main".to_string(),
                0,
            ),
        );

        let result = invoker.invoke_pipeline_plugin("rust_plugin", &ctx).await;
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(err.code.as_ref().unwrap().contains("INPROCESS"));
    }

    #[tokio::test]
    async fn test_invoke_nonexistent_plugin() {
        let loader = Arc::new(MockLoader::new());
        let invoker = PluginInvokerImpl::new(loader);
        let ctx = PluginContext::new(
            json!({}),
            json!({}),
            TenantContext::new("t1", "s1"),
            Uuid::new_v4(),
            lingxi_core::types::ContentLoader::new(
                std::sync::Arc::new(MockStorage),
                "run1".to_string(),
                "main".to_string(),
                0,
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
            id: "composite_test".to_string(),
            name: "Composite".to_string(),
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Composite,
            pipeline_role: None,
            language: "yaml".to_string(),
            host_type: HostType::InProcess,
            entry: String::new(),
            capabilities: Default::default(),
            dependencies: vec![],
            permissions: Default::default(),
            error_policy: Default::default(),
            priority: 100,
            mcp: None,
            requires_content: None,
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
    async fn test_check_health_not_connected() {
        let loader = Arc::new(MockLoader::new());
        let invoker = PluginInvokerImpl::new(loader);
        assert!(!invoker.check_health("nonexistent").await);
    }

    #[tokio::test]
    async fn test_force_unload_nonexistent() {
        let loader = Arc::new(MockLoader::new());
        let invoker = PluginInvokerImpl::new(loader);
        // force_unload 对不存在的插件也应该返回 Ok
        let result = invoker.force_unload("nonexistent").await;
        assert!(result.is_ok());
    }

    // Mock StorageBackend for test context
    struct MockStorage;

    #[async_trait::async_trait]
    impl lingxi_core::traits::StorageBackend for MockStorage {
        async fn get_run(
            &self,
            _run_id: &str,
        ) -> Result<lingxi_core::types::RunRecord, lingxi_core::types::StorageError> {
            Err(lingxi_core::types::StorageError::NotFound(
                "mock".to_string(),
            ))
        }
        async fn get_messages(
            &self,
            _run_id: &str,
            _branch_id: &str,
        ) -> Result<Vec<lingxi_core::types::MessageRecord>, lingxi_core::types::StorageError>
        {
            Ok(vec![])
        }
        async fn get_recent_messages(
            &self,
            _run_id: &str,
            _branch_id: &str,
            _n: usize,
        ) -> Result<Vec<lingxi_core::types::Message>, lingxi_core::types::StorageError> {
            Ok(vec![])
        }
        async fn get_blob(
            &self,
            _blob_id: &str,
        ) -> Result<Vec<u8>, lingxi_core::types::StorageError> {
            Ok(vec![])
        }
        async fn append_trace(
            &self,
            _entry: lingxi_core::types::TraceEntry,
        ) -> Result<(), lingxi_core::types::StorageError> {
            Ok(())
        }
        async fn create_branch(
            &self,
            _branch: lingxi_core::types::Branch,
        ) -> Result<(), lingxi_core::types::StorageError> {
            Ok(())
        }
        async fn update_run_status(
            &self,
            _run_id: &str,
            _status: lingxi_core::types::RunStatus,
            _branch: Option<&str>,
            _seq: Option<u32>,
        ) -> Result<(), lingxi_core::types::StorageError> {
            Ok(())
        }
    }
}
