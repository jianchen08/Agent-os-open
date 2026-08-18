//! 插件生命周期公共逻辑（启动期注册 + 运行时新增插件注册）。
//!
//! 把 main 启动期"遍历 manifest 注册 tools/route_signals 到 capability_registry"的逻辑
//! 抽成公共函数，供：
//! - 启动期（agentos-kernel.rs）
//! - 运行时新增插件（reload-all 端点发现新插件后注册）
//!
//! 复用，避免逻辑重复。

use agentos_core::traits::{CapabilityRegistry, PluginManifest, ToolDescriptor};
use agentos_core::types::{ToolCategory, ToolSource};
use agentos_plugin_loader::{CapabilityRegistryImpl, PluginScopeRegistry};
use std::sync::Arc;

/// 把单个插件的 tools 和 route_signals 注册到 capability_registry。
///
/// D.6 槽位拆分（2026-08-15，废止原附录D① 类型门控）：`capabilities.tools`
/// 语义唯一化 = 给 LLM 的工具，**声明即注册**，不再看 plugin_type。
/// 多职能插件（system 适配器声明 tools + services 两块）自然成立；
/// 内部服务方法声明在 `capabilities.services`，不进本注册（调用走
/// invoke_entry / http_endpoints / 显式 plugin_id / provides 通道）。
///
/// M1：`scopes` 为 Some 时经 guarded 注册并把撤销 guard 登记进该插件的 PluginScope
/// （disable/unload 时结构性收回）；None 时走旧路径（行为不变，测试/兼容用）。
/// 返回注册的 tool 数量。
pub fn register_plugin_capabilities(
    manifest: &PluginManifest,
    registry: &Arc<CapabilityRegistryImpl>,
    scopes: Option<&PluginScopeRegistry>,
) -> usize {
    let mut count = 0usize;
    let scope = scopes.map(|s| s.scope_of(&manifest.id));
    {
        for tool_cap in &manifest.capabilities.tools {
            let category = tool_cap.category.clone().unwrap_or(ToolCategory::System);
            let descriptor = ToolDescriptor {
                name: tool_cap.name.clone(),
                description: tool_cap
                    .description
                    .clone()
                    .unwrap_or_else(|| format!("Tool from {}", manifest.name)),
                plugin_id: manifest.id.clone(),
                input_schema: tool_cap
                    .input_schema
                    .clone()
                    .unwrap_or(serde_json::json!({})),
                output_schema: tool_cap.output_schema.clone(),
                category,
                source: if manifest.host_type == agentos_core::traits::HostType::Sidecar {
                    ToolSource::Mcp
                } else {
                    ToolSource::Builtin
                },
                ui: tool_cap.ui.clone(),
                render: tool_cap.render.clone(),
            };
            match &scope {
                Some(s) => s.track(registry.register_tool_guarded(&manifest.id, descriptor)),
                None => registry.register_tool(&manifest.id, descriptor),
            }
            count += 1;
        }
    }

    // 注册路由信号
    if !manifest.capabilities.route_signals.is_empty() {
        match &scope {
            Some(s) => s.track(registry.register_route_signals_guarded(
                &manifest.id,
                manifest.capabilities.route_signals.clone(),
            )),
            None => registry
                .register_route_signals(&manifest.id, manifest.capabilities.route_signals.clone()),
        }
    }

    count
}

/// 批量注册多个插件（启动期或 reload-all 发现新增时用）。
///
/// `existing_ids`：已注册的 plugin_id 集合，用于跳过重复（仅注册新增的）。
/// 返回 (新增注册的插件 id 列表, 注册的 tool 总数)。
pub fn register_new_plugins(
    all_manifests: &[PluginManifest],
    existing_ids: &std::collections::HashSet<String>,
    registry: &Arc<CapabilityRegistryImpl>,
    scopes: Option<&PluginScopeRegistry>,
) -> (Vec<String>, usize) {
    let mut new_ids = Vec::new();
    let mut total_tools = 0usize;
    for manifest in all_manifests {
        if existing_ids.contains(&manifest.id) {
            continue;
        }
        total_tools += register_plugin_capabilities(manifest, registry, scopes);
        new_ids.push(manifest.id.clone());
    }
    (new_ids, total_tools)
}

/// 判断插件是否声明了 http_endpoints（用于 reload-all 的诚实降级提示）。
pub fn has_http_endpoints(manifest: &PluginManifest) -> bool {
    !manifest.http_endpoints.is_empty()
}

/// 插件重新启用后立即重注册其能力（G1 enable 对称化）。
///
/// 禁用路径 `clear_plugin` 按插件清四维；启用侧无需重启——`/ext/{*rest}`
/// 通配分发是注册表数据驱动，路由树无需重建。
/// 本函数补齐对称：tools + route_signals（经 [`register_plugin_capabilities`]）+
/// http_endpoints（对齐 watcher `apply_discovered_plugins` 的补注册段）。
///
/// 幂等：对已启用插件重复调用，tools 覆盖同名、http 路由同 path+method 冲突时忽略。
/// M1：`scopes` 为 Some 时先 revoke 该插件旧 scope（清残留 guard）再 guarded 重注册。
/// 返回 (注册的 tool 数, 注册的 http 路由数)。
pub fn reenable_plugin_capabilities(
    manifest: &PluginManifest,
    registry: &Arc<CapabilityRegistryImpl>,
    scopes: Option<&PluginScopeRegistry>,
) -> (usize, usize) {
    if let Some(s) = scopes {
        // 幂等基线：先收回旧 scope 的全部登记（禁用遗留或重复启用），
        // 再重注册拿全新 guard——避免 by_plugin 索引重复条目。
        s.revoke(&manifest.id);
    }
    let tools = register_plugin_capabilities(manifest, registry, scopes);
    let scope = scopes.map(|s| s.scope_of(&manifest.id));
    let mut http_routes = 0usize;
    for ep in &manifest.http_endpoints {
        let ok = match &scope {
            Some(s) => registry
                .register_http_route_guarded(&manifest.id, ep.clone())
                .map(|(_d, guard)| s.track(guard))
                .is_ok(),
            None => registry
                .register_http_route(&manifest.id, ep.clone())
                .is_ok(),
        };
        if ok {
            http_routes += 1;
        }
    }
    (tools, http_routes)
}

/// 广播域事件（通用域事件通道：`LifecycleHook::DomainEvent` + `ctx["event"]` 事件名）。
///
/// 两条投递路径：
/// - **观察总线**（audit/metrics 订阅者）：经 [`agentos_hooks::global`] 广播，
///   best-effort 非阻塞；总线未注册（测试/降级）时静默跳过。
/// - **点对点**：manifest `capabilities.lifecycle_hooks` 声明了 `domain_event`
///   且已启用的插件，经 `send_lifecycle_hook` 收到 `notifications/domain_event`
///   （sidecar 侧 SDK 自动分发到 `on_domain_event` 处理器）。fire-and-forget
///   spawn——通知失败只告警、绝不阻塞调用方主流程；副作用是声明订阅的
///   sidecar 可能因通知被懒 spawn，这是订阅的既定代价。
///
/// 发射点：会话创建/删除（session_routes）、活跃会话切换（ws_session）。
pub async fn broadcast_domain_event(
    state: &crate::routes::AppState,
    name: &str,
    tags: Vec<(&str, serde_json::Value)>,
) {
    let Some(invoker) = state.invoker.clone() else {
        return;
    };
    let enabled = state.enabled_plugin_ids.clone();
    let manifests = state.manifests.clone();
    broadcast_domain_event_from(&invoker, &enabled, &manifests, name, tags).await;
}

/// 域事件投递（broadcast_domain_event 的组件版——供不持 AppState 的调用方
/// 复用，如 capability_router 的域事件广播闭包）。双通道：观察总线 +
/// 点对点推给声明 domain_event 的启用插件。
pub async fn broadcast_domain_event_from(
    invoker: &Arc<dyn agentos_core::traits::PluginInvoker>,
    enabled: &tokio::sync::RwLock<std::collections::HashSet<String>>,
    manifests: &tokio::sync::RwLock<Vec<agentos_core::traits::PluginManifest>>,
    name: &str,
    tags: Vec<(&str, serde_json::Value)>,
) {
    if let Some(bus) = agentos_hooks::global() {
        bus.emit(agentos_hooks::domain_event(name, tags.clone()));
    }
    let enabled = enabled.read().await;
    let manifests = manifests.read().await;
    for manifest in manifests.iter() {
        if !enabled.contains(&manifest.id) {
            continue;
        }
        if !manifest
            .capabilities
            .lifecycle_hooks
            .contains(&agentos_core::traits::LifecycleHook::DomainEvent)
        {
            continue;
        }
        let mut ctx = agentos_core::traits::HookContext::new();
        ctx.set("event", serde_json::json!(name));
        for (key, value) in tags.clone() {
            ctx.set(key, value);
        }
        let plugin_id = manifest.id.clone();
        let event_name = name.to_string();
        let inv = invoker.clone();
        tokio::spawn(async move {
            if let Err(e) = inv
                .send_lifecycle_hook(
                    &plugin_id,
                    agentos_core::traits::LifecycleHook::DomainEvent,
                    &ctx,
                )
                .await
            {
                tracing::warn!(
                    plugin = %plugin_id,
                    event = %event_name,
                    error = %e.message,
                    "域事件点对点通知失败"
                );
            }
        });
    }
}

#[cfg(test)]
mod domain_event_tests {
    use super::broadcast_domain_event;
    use crate::routes::AppState;
    use agentos_core::traits::{
        HookContext, HostType, LifecycleHook, ManifestCapabilities, PluginInvoker, PluginManifest,
        PluginType,
    };
    use agentos_core::types::{PluginContext, PluginError, PluginResult, ToolExecutionResult};
    use serde_json::json;
    use std::collections::HashSet;
    use std::sync::{Arc, Mutex};

    /// 记录 send_lifecycle_hook 调用的 mock invoker（invoke_* 本测试不可达）。
    struct RecordingInvoker {
        hooks: Mutex<Vec<(String, String)>>, // (plugin_id, event)
    }

    #[async_trait::async_trait]
    impl PluginInvoker for RecordingInvoker {
        async fn invoke_pipeline_plugin(
            &self,
            _plugin_id: &str,
            _ctx: &PluginContext,
        ) -> Result<PluginResult, PluginError> {
            unimplemented!("域事件测试不触达")
        }
        async fn invoke_tool(
            &self,
            _plugin_id: &str,
            _tool_name: &str,
            _inputs: &serde_json::Value,
        ) -> Result<ToolExecutionResult, PluginError> {
            unimplemented!("域事件测试不触达")
        }
        async fn send_lifecycle_hook(
            &self,
            plugin_id: &str,
            hook: LifecycleHook,
            context: &HookContext,
        ) -> Result<(), PluginError> {
            assert_eq!(hook, LifecycleHook::DomainEvent);
            let event = context
                .get("event")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            self.hooks
                .lock()
                .unwrap()
                .push((plugin_id.to_string(), event));
            Ok(())
        }
    }

    fn manifest(id: &str, declare_domain: bool) -> PluginManifest {
        PluginManifest {
            id: id.to_string(),
            name: id.to_string(),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::System,
            pipeline_role: None,
            language: "python".to_string(),
            host_type: HostType::Sidecar,
            entry: "python server.py".to_string(),
            capabilities: ManifestCapabilities {
                lifecycle_hooks: if declare_domain {
                    vec![LifecycleHook::DomainEvent]
                } else {
                    vec![]
                },
                ..Default::default()
            },
            dependencies: vec![],
            permissions: Default::default(),
            error_policy: Default::default(),
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
            persistent_fields: vec![],
            provides: None,
        }
    }

    #[tokio::test]
    async fn domain_event_reaches_declaring_enabled_plugins_only() {
        let invoker = Arc::new(RecordingInvoker {
            hooks: Mutex::new(Vec::new()),
        });
        let mut state = AppState::with_config(json!({}));
        // A：声明 + 启用 → 收到；B：未声明 → 不收到；C：声明但禁用 → 不收到
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![
            manifest("p_a", true),
            manifest("p_b", false),
            manifest("p_c", true),
        ]));
        state.enabled_plugin_ids = Arc::new(tokio::sync::RwLock::new(HashSet::from([
            "p_a".to_string(),
            "p_b".to_string(),
        ])));
        state.invoker = Some(invoker.clone());

        broadcast_domain_event(
            &state,
            "session.created",
            vec![("session_id", json!("t1")), ("pipeline_id", json!("pipe1"))],
        )
        .await;

        // 点对点是 fire-and-forget spawn：轮询等任务落地
        for _ in 0..200 {
            if !invoker.hooks.lock().unwrap().is_empty() {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        let got = invoker.hooks.lock().unwrap().clone();
        assert_eq!(
            got,
            vec![("p_a".to_string(), "session.created".to_string())],
            "只投递给声明了 domain_event 且启用的插件"
        );
    }
}
