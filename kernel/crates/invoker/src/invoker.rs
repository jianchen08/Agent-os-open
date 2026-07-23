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
use agentos_core::traits::{
    HookContext, HostType, LifecycleHook, PluginInvoker, PluginLoader, PluginManifest, PluginType,
};
use agentos_core::types::{PluginContext, PluginError, PluginResult, ToolExecutionResult};
use agentos_mcp::{CapabilityRouter, McpClient, McpError};
use parking_lot::RwLock;
use tracing::{error, info, warn};

/// 从 MCP tools/call 响应中提取内部 JSON 值。
///
/// Python SDK 的 McpServer 返回格式为：
/// ```json
/// { "content": [{ "type": "text", "text": "<json_string>" }], "isError": false }
/// ```
/// 其中 `text` 字段是工具实际返回值的 JSON 字符串。
/// 本函数提取 `content[0].text`，解析为 `serde_json::Value` 返回。
///
/// 如果 `isError` 为 true 或解析失败，返回包含错误信息的 JSON 对象。
fn extract_mcp_content(mcp_result: &serde_json::Value) -> serde_json::Value {
    // 检查 isError 标志
    if mcp_result
        .get("isError")
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
    {
        let err_msg = mcp_result
            .get("content")
            .and_then(|c| c.as_array())
            .and_then(|arr| arr.first())
            .and_then(|item| item.get("text"))
            .and_then(|t| t.as_str())
            .unwrap_or("MCP tool returned isError=true");
        return serde_json::json!({"error": err_msg});
    }

    // 提取 content[0].text 并解析为 JSON
    let extracted = mcp_result
        .get("content")
        .and_then(|c| c.as_array())
        .and_then(|arr| arr.first())
        .and_then(|item| item.get("text"))
        .and_then(|t| t.as_str())
        .and_then(|s| serde_json::from_str(s).ok());

    match extracted {
        Some(val) => val,
        None => {
            warn!(
                "MCP response content extraction failed, returning raw result: {:?}",
                mcp_result
            );
            mcp_result.clone()
        }
    }
}

/// 按插件的 `config_refs` 声明过滤全量配置，只保留声明需要的顶层配置节。
///
/// 配置按需注入（ADR 配置统一）：每个插件通过 manifest 的 `config_refs`
/// 声明它需要读取哪些配置节（对应配置文件/目录的顶层 key，如 `models`、`system`）。
///
/// - `refs` 为空 → 返回全量配置（向后兼容未声明 `config_refs` 的旧插件）。
/// - `refs` 非空 → 仅保留 `full_config` 中 key 出现在 `refs` 里的顶层字段；
///   `refs` 中声明但全量配置里不存在的 key 静默跳过（插件拿不到该节，消费方需自行降级）。
///
/// 这样避免把全系统配置（含其他插件的凭证/密钥）泄漏给每个 sidecar。
fn filter_config_by_refs(full_config: &serde_json::Value, refs: &[String]) -> serde_json::Value {
    if refs.is_empty() {
        return full_config.clone();
    }
    let Some(obj) = full_config.as_object() else {
        return full_config.clone();
    };
    let mut filtered = serde_json::Map::new();
    for key in refs {
        if let Some(val) = obj.get(key) {
            filtered.insert(key.clone(), val.clone());
        }
    }
    serde_json::Value::Object(filtered)
}

/// 构造注入给插件的配置（P1-2：config_files 优先，无则回退 config_refs）。
///
/// - manifest 声明了 `config_files`：按 `config_files[].id` 命名空间合并（B3），
///   每个 id 对应的值是 `config_files[].path` 在 `full_config` 递归扫描结果中的定位。
///   `full_config` 是 loader 对 `config/` 根的递归扫描：`config/models/llm.yaml`
///   → `full_config["models"]["llm"]`。路径解析见 [`resolve_config_path`]。
/// - 未声明 `config_files`：回退到 [`filter_config_by_refs`]（迁移期 config_refs 旧路径，
///   P6 才删除）。
///
/// 设计依据：ADR §4.3 B3（合并 dict 的命名空间）+ §8.2 step1（P1 只增不删）。
fn build_injected_config(
    full_config: &serde_json::Value,
    manifest: &PluginManifest,
) -> serde_json::Value {
    if manifest.config_files.is_empty() {
        return filter_config_by_refs(full_config, &manifest.config_refs);
    }
    let mut merged = serde_json::Map::new();
    for mapping in &manifest.config_files {
        let value = resolve_config_path(full_config, &mapping.path)
            .cloned()
            .unwrap_or_else(|| serde_json::json!({}));
        merged.insert(mapping.id.clone(), value);
    }
    serde_json::Value::Object(merged)
}

/// 按 config_files[].path 在递归扫描的 full_config 中定位文件内容。
///
/// 路径归一化：
/// - 去掉开头的 `config/` 前缀（manifest 可写 `config/models/llm.yaml` 或 `models/llm.yaml`）；
/// - 去掉 `.yaml`/`.yml` 扩展名；
/// - 按 `/` 分割为嵌套 key 序列，逐层下钻 `full_config`。
///
/// 任一层不存在 → 返回 None（调用方降级为空 dict）。
fn resolve_config_path<'a>(
    full_config: &'a serde_json::Value,
    path: &str,
) -> Option<&'a serde_json::Value> {
    let normalized = path.trim_start_matches("config/").trim_start_matches("config\\");
    let no_ext = normalized
        .strip_suffix(".yaml")
        .or_else(|| normalized.strip_suffix(".yml"))
        .unwrap_or(normalized);
    let mut current = full_config;
    for seg in no_ext.replace('\\', "/").split('/') {
        if seg.is_empty() {
            continue;
        }
        current = current.get(seg)?;
    }
    Some(current)
}

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
    /// Capability 路由器——sidecar→内核反向调用通道。
    /// 设置后，新建的 MCP 客户端会带上路由器；已有客户端需重连才生效。
    router: RwLock<Option<Arc<dyn CapabilityRouter>>>,
}

impl PluginInvokerImpl {
    /// 创建 PluginInvoker。
    pub fn new(loader: Arc<dyn PluginLoader>) -> Self {
        Self {
            loader,
            mcp_clients: RwLock::new(HashMap::new()),
            crash_callbacks: RwLock::new(Vec::new()),
            router: RwLock::new(None),
        }
    }

    /// 设置 Capability 路由器（启用 sidecar→内核反向调用）。
    ///
    /// 必须在 engine 创建后调用（路由器需要 engine 句柄）。
    /// 之后新建的 MCP 客户端会自动带上路由器；已连接的客户端下次重连时生效。
    pub fn set_router(&self, router: Arc<dyn CapabilityRouter>) {
        *self.router.write() = Some(router);
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

        // 应用 Capability 路由器（启用 sidecar→内核反向调用通道）
        {
            let router_guard = self.router.read();
            if let Some(router) = router_guard.as_ref() {
                client = client.with_router(Arc::clone(router));
            }
        }

        // 设置工作目录为插件目录（确保 server.py 等相对路径可解析）
        if let Some(plugin_dir) = self.loader.get_plugin_dir(&manifest.id) {
            client = client.with_working_dir(plugin_dir);
        }

        // 注入 PYTHONPATH：把项目 src/ 加进子进程搜索路径，
        // 让 sidecar 的 plugin.py 能 import 公共业务包（tools/memory/llm 等）。
        // 从 AGENTOS_PLUGINS_DIR（plugins/shared）推算项目根 → src/
        let mut extra_env: Vec<(String, String)> = Vec::new();
        if let Ok(plugins_dir) = std::env::var("AGENTOS_PLUGINS_DIR") {
            let plugins_path = std::path::Path::new(&plugins_dir);
            // plugins/shared → plugins/ → 项目根
            if let Some(project_root) = plugins_path
                .parent() // plugins/
                .and_then(|p| p.parent()) // 项目根
            {
                let src_dir = project_root.join("src");
                if src_dir.is_dir() {
                    let existing = std::env::var("PYTHONPATH").unwrap_or_default();
                    let new_path = if existing.is_empty() {
                        src_dir.to_string_lossy().to_string()
                    } else {
                        format!(
                            "{}{}{}",
                            src_dir.to_string_lossy(),
                            std::path::MAIN_SEPARATOR,
                            existing
                        )
                    };
                    extra_env.push(("PYTHONPATH".to_string(), new_path));
                }
            }
        }
        if !extra_env.is_empty() {
            client = client.with_extra_env(extra_env);
        }

        client.connect().await.map_err(|e| PluginError {
            message: format!("MCP connect failed: {}", e),
            code: Some("MCP_CONNECT_FAILED".to_string()),
            source: Some("plugin-invoker".to_string()),
        })?;

        // initialize 握手（携带插件配置）
        // 配置加载失败时分级处理：IO 错误（目录不存在等）可降级为空配置；
        // 解析错误（YAML 语法错误）应报错，让插件启动失败比悄悄降级更安全。
        let full_config = match self.loader.load_config().await {
            Ok(config) => config,
            Err(e) => {
                if e
                    .code
                    .as_deref()
                    .map(|c| c.contains("PARSE"))
                    .unwrap_or(false)
                {
                    return Err(PluginError {
                        message: format!("Plugin config parse error: {}", e),
                        code: Some("CONFIG_PARSE_ERROR".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    });
                }
                warn!("Failed to load plugin config, using empty: {}", e);
                serde_json::json!({})
            }
        };
        // 按需注入（ADR 配置统一）：优先 config_files 映射（P1-2），无则回退 config_refs。
        // 避免把全系统配置（含其他插件凭证）泄漏给每个 sidecar。
        let config = build_injected_config(&full_config, manifest);
        client.initialize(&config).await.map_err(|e| PluginError {
            message: format!("MCP initialize failed: {}", e),
            code: Some("MCP_INIT_FAILED".to_string()),
            source: Some("plugin-invoker".to_string()),
        })?;

        // 发送 on_load 通知——触发 Python 插件的 @plugin.on_load 回调，
        // 初始化插件实例（如 _instance = MyPlugin(config)）。
        // 不等待响应（fire-and-forget）；失败仅 warn 不阻断。
        let _ = client
            .send_notification("notifications/on_load", Some(config.clone()))
            .await
            .inspect_err(|e| {
                warn!("on_load notification failed for {}: {}", manifest.id, e)
            });

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

    /// 插件权限声明的前置日志校验（P2-2）。
    ///
    /// 0.2 只做声明 + 日志告警，不做硬 enforce（filesystem/system_calls 留 0.3 沙箱）。
    /// 当前检测项：
    /// - 若 manifest 声明了 network 权限（`permissions.network.allowed_hosts` 非空），
    ///   则认为该插件可能联网；这是声明性记录，不阻断调用。
    /// - 若 manifest 同时声明了 network 权限但 `allowed_hosts` 为空，
    ///   说明声明含糊（声称要联网却未指定可信主机），记 warning。
    ///
    /// 该函数不返回错误，**永远不阻断** invoke 流程。
    fn check_permissions(&self, plugin_id: &str, manifest: &PluginManifest) {
        let perms = &manifest.permissions;

        // 记录声明：network/filesystem/env_vars/system_calls 是否声明
        let has_network = !perms.network.allowed_hosts.is_empty();
        let has_fs =
            !perms.filesystem.read_paths.is_empty() || !perms.filesystem.write_paths.is_empty();
        let has_env = !perms.env_vars.is_empty();
        let has_syscalls = !perms.system_calls.is_empty();

        info!(
            plugin_id = plugin_id,
            network = has_network,
            filesystem = has_fs,
            env_vars = has_env,
            system_calls = has_syscalls,
            "Plugin permission declaration"
        );

        // 声明含糊检测：声明了要联网（有非空 host 列表）说明确实要联网；
        // 若声明了 network 权限意图但 allowed_hosts 为空，
        // 说明声明不完整，记 warning（不阻断）。
        // 注：0.2 不强制 enforce，仅日志留痕供审计。
        if has_network {
            info!(
                plugin_id = plugin_id,
                hosts = ?perms.network.allowed_hosts,
                "Plugin declared network access"
            );
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

        // P2-2 插件权限声明前置日志校验（不阻断）
        self.check_permissions(plugin_id, manifest);

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

                // 从 manifest 获取工具名——插件注册的 tool name 是
                // "<plugin_id>.execute" 格式（如 "context_build.execute"）
                let tool_name = manifest
                    .capabilities
                    .tools
                    .first()
                    .map(|t| t.name.as_str())
                    .unwrap_or("execute");

                // 调用 tools/call
                let tool_args = serde_json::json!({
                    "state": ctx.state,
                    "config": ctx.config,
                });

                let result = client.call_tool(tool_name, &tool_args).await.map_err(|e| {
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

                // 解析 MCP 响应——Python SDK 返回格式为：
                // { "content": [{ "type": "text", "text": "<json_string>" }], "isError": false }
                // 提取 content[0].text 并反序列化为 PluginResult
                let inner = extract_mcp_content(&result);
                let plugin_result: PluginResult = serde_json::from_value(inner).map_err(|e| {
                    PluginError {
                        message: format!("failed to parse MCP response as PluginResult: {}", e),
                        code: Some("PARSE_ERROR".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    }
                })?;

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

        // P2-2 插件权限声明前置日志校验（不阻断）
        self.check_permissions(plugin_id, manifest);

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

                // 解析 MCP 响应——与 pipeline 调用使用相同的解析逻辑
                let inner = extract_mcp_content(&result);
                let tool_result: ToolExecutionResult = serde_json::from_value(inner).map_err(|e| {
                    PluginError {
                        message: format!("failed to parse MCP response as ToolExecutionResult: {}", e),
                        code: Some("PARSE_ERROR".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    }
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
    use agentos_core::traits::{ConfigFileMapping, LoadedPlugin, PluginManifest, PluginStatus};
    use agentos_core::types::TenantContext;
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
            config_refs: vec![],
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
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
            config_refs: vec![],
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
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
            agentos_core::types::ContentLoader::new(
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
            agentos_core::types::ContentLoader::new(
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
            config_refs: vec![],
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
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

    // ── filter_config_by_refs 按需注入过滤单元测试 ──

    #[test]
    fn test_filter_config_empty_refs_returns_full_config() {
        // 空 refs → 返回全量配置（向后兼容未声明 config_refs 的旧插件）
        let full = json!({
            "models": {"llm": {"name": "glm"}},
            "system": {"timeout": 30},
            "secrets": {"api_key": "leak"}
        });
        let filtered = filter_config_by_refs(&full, &[]);
        assert_eq!(filtered, full, "empty refs should return full config unchanged");
    }

    #[test]
    fn test_filter_config_with_refs_keeps_only_declared_keys() {
        // 声明 refs → 只保留声明 key，未声明的 key（如 secrets）被剔除
        let full = json!({
            "models": {"llm": {"name": "glm"}},
            "system": {"timeout": 30},
            "secrets": {"api_key": "leak"}
        });
        let refs = vec!["models".to_string(), "system".to_string()];
        let filtered = filter_config_by_refs(&full, &refs);
        let obj = filtered.as_object().unwrap();
        assert_eq!(obj.len(), 2, "should keep only declared keys");
        assert!(obj.contains_key("models"));
        assert!(obj.contains_key("system"));
        assert!(
            !obj.contains_key("secrets"),
            "undeclared keys must be filtered out"
        );
        // 保留的 key 内容应与原配置一致
        assert_eq!(filtered["models"]["llm"]["name"], "glm");
        assert_eq!(filtered["system"]["timeout"], 30);
    }

    #[test]
    fn test_filter_config_skips_nonexistent_refs() {
        // refs 中声明但全量配置不存在的 key 静默跳过
        let full = json!({
            "models": {"llm": {"name": "glm"}}
        });
        let refs = vec![
            "models".to_string(),
            "nonexistent".to_string(),
            "memory_storage".to_string(),
        ];
        let filtered = filter_config_by_refs(&full, &refs);
        let obj = filtered.as_object().unwrap();
        assert_eq!(obj.len(), 1, "only existing declared keys are kept");
        assert!(obj.contains_key("models"));
        assert!(!obj.contains_key("nonexistent"));
        assert!(!obj.contains_key("memory_storage"));
    }

    // ── P1-2 build_injected_config：config_files 优先 / config_refs 回退 ──

    /// 辅助：构造一个带 config_files 的 manifest。
    fn make_manifest_with_config_files(id: &str, files: Vec<ConfigFileMapping>) -> PluginManifest {
        PluginManifest {
            id: id.to_string(),
            name: format!("Test {}", id),
            version: "1.0.0".to_string(),
            plugin_type: PluginType::System,
            pipeline_role: None,
            language: "python".to_string(),
            host_type: HostType::Sidecar,
            entry: "python server.py".to_string(),
            capabilities: Default::default(),
            dependencies: vec![],
            permissions: Default::default(),
            error_policy: Default::default(),
            priority: 100,
            mcp: None,
            requires_content: None,
            config_refs: vec![],
            config_files: files,
            http_endpoints: vec![],
            ui_schema: None,
        }
    }

    /// 有 config_files 时，按 config_files[].id 命名空间合并（B3）。
    /// full_config 是 config_root 递归扫描结果：config/models/llm.yaml → models.llm。
    #[test]
    fn test_build_injected_config_uses_config_files_namespaced() {
        let full = json!({
            "models": {
                "llm": {"default_model": "glm"},
                "embedding": {"dim": 1024}
            },
            "external_tools": {
                "godot": {"endpoint": "http://localhost:9600"},
                "vscode": {"endpoint": "http://localhost:9741"}
            }
        });
        let manifest = make_manifest_with_config_files(
            "llm_service",
            vec![
                ConfigFileMapping {
                    id: "llm".to_string(),
                    path: "config/models/llm.yaml".to_string(),
                    label: "LLM".to_string(),
                },
                ConfigFileMapping {
                    id: "embedding".to_string(),
                    path: "config/models/embedding.yaml".to_string(),
                    label: "Embedding".to_string(),
                },
            ],
        );

        let injected = build_injected_config(&full, &manifest);
        let obj = injected.as_object().unwrap();
        // key = config_files[].id，不是文件 stem
        assert_eq!(obj.len(), 2, "should merge by config_files[].id");
        assert_eq!(injected["llm"]["default_model"], "glm");
        assert_eq!(injected["embedding"]["dim"], 1024);
    }

    /// 无 config_files 时回退到 config_refs 旧路径（迁移期并存）。
    #[test]
    fn test_build_injected_config_falls_back_to_config_refs() {
        let full = json!({
            "models": {"llm": {"name": "glm"}},
            "secrets": {"api_key": "leak"}
        });
        let mut manifest = make_manifest_with_config_files("memory", vec![]);
        manifest.config_refs = vec!["models".to_string()];

        let injected = build_injected_config(&full, &manifest);
        // 回退到 filter_config_by_refs：只保留 models，剔除 secrets
        assert_eq!(injected["models"]["llm"]["name"], "glm");
        assert!(
            injected.as_object().unwrap().get("secrets").is_none(),
            "config_refs path must still filter"
        );
    }

    /// config_files 声明的文件在 full_config 不存在时，该 id 对应空 dict（不崩）。
    #[test]
    fn test_build_injected_config_missing_file_yields_empty_dict() {
        let full = json!({"models": {"llm": {"name": "glm"}}});
        let manifest = make_manifest_with_config_files(
            "p",
            vec![ConfigFileMapping {
                id: "nope".to_string(),
                path: "config/models/nope.yaml".to_string(),
                label: "Nope".to_string(),
            }],
        );

        let injected = build_injected_config(&full, &manifest);
        assert_eq!(injected["nope"], json!({}), "missing file maps to empty dict");
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
        async fn get_messages(
            &self,
            _run_id: &str,
            _branch_id: &str,
        ) -> Result<Vec<agentos_core::types::MessageRecord>, agentos_core::types::StorageError>
        {
            Ok(vec![])
        }
        async fn get_recent_messages(
            &self,
            _run_id: &str,
            _branch_id: &str,
            _n: usize,
        ) -> Result<Vec<agentos_core::types::Message>, agentos_core::types::StorageError> {
            Ok(vec![])
        }
        async fn get_blob(
            &self,
            _blob_id: &str,
        ) -> Result<Vec<u8>, agentos_core::types::StorageError> {
            Ok(vec![])
        }
        async fn append_trace(
            &self,
            _entry: agentos_core::types::TraceEntry,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        async fn create_branch(
            &self,
            _branch: agentos_core::types::Branch,
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
    }
}
