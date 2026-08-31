//! 插件生命周期公共逻辑（启动期注册 + 运行时新增插件注册）。
//!
//! 把 main 启动期"遍历 manifest 注册 tools/route_signals 到 capability_registry"的逻辑
//! 抽成公共函数，供：
//! - 启动期（agentos-kernel.rs）
//! - 运行时新增插件（reload-all 端点发现新插件后注册）
//!
//! 复用，避免逻辑重复。

use agentos_core::traits::{PluginManifest, ToolDescriptor};
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
/// M1：经 guarded 注册并把撤销 guard 登记进该插件的 PluginScope
/// （disable/unload 时结构性收回）。
/// 返回注册的 tool 数量。
pub fn register_plugin_capabilities(
    manifest: &PluginManifest,
    registry: &Arc<CapabilityRegistryImpl>,
    scopes: &PluginScopeRegistry,
) -> usize {
    let mut count = 0usize;
    let scope = scopes.scope_of(&manifest.id);
    {
        for tool_cap in &manifest.capabilities.tools {
            let category = tool_cap.category.clone().unwrap_or(ToolCategory::System);
            // K9 + 强制规则：input_schema 缺失时的行为按宿主分档——
            //   - external MCP 工具（entry="mcp:external"）：**拒注册**。此类工具
            //     的 manifest 声明是 LLM 工具面 input_schema 的唯一真值源（G2 只
            //     比对、不回填握手 schema），缺声明 = 注册出 {} = LLM 收到零参数
            //     工具，调用必因缺参被服务端校验拒绝；拒注册直接暴露问题，
            //     不给"盲调工具"留后门。
            //   - 内置/sidecar 自研（http.handle、*.status 哨兵、widget_demo 演示
            //     工具）：零参合法（HTTP 处理器/无参状态查询），维持 {} 补注册，
            //     仅 warn（K9 既有行为）。
            let is_external_mcp = manifest.entry == "mcp:external";
            if tool_cap.input_schema.is_none() {
                if is_external_mcp {
                    tracing::error!(
                        target: "plugin-registration",
                        plugin_id = %manifest.id,
                        tool = %tool_cap.name,
                        "external MCP 工具缺 input_schema，拒绝注册（LLM 工具面唯一真值源缺失，注册即零参数盲调；请按 MCP tools/list inputSchema 补声明）"
                    );
                    continue;
                }
                tracing::warn!(
                    target: "plugin-registration",
                    plugin_id = %manifest.id,
                    tool = %tool_cap.name,
                    "tool manifest 缺 input_schema，以 {{}} 补注册（LLM 侧 object 过滤恒不触发，LLM 只能盲调；请补声明）"
                );
            }
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
            scope.track(registry.register_tool_guarded(&manifest.id, descriptor));
            count += 1;
        }
    }

    // 注册路由信号
    if !manifest.capabilities.route_signals.is_empty() {
        scope.track(registry.register_route_signals_guarded(
            &manifest.id,
            manifest.capabilities.route_signals.clone(),
        ));
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
    scopes: &PluginScopeRegistry,
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

/// 插件重新启用后立即重注册其能力（G1 enable 对称化）。
///
/// 禁用路径 `clear_plugin` 按插件清四维；启用侧无需重启——`/ext/{*rest}`
/// 通配分发是注册表数据驱动，路由树无需重建。
/// 本函数补齐对称：tools + route_signals（经 [`register_plugin_capabilities`]）+
/// http_endpoints（对齐 watcher `apply_discovered_plugins` 的补注册段）。
///
/// 幂等：对已启用插件重复调用，tools 覆盖同名、http 路由同 path+method 冲突时忽略。
/// M1：先 revoke 该插件旧 scope（清残留 guard）再 guarded 重注册。
/// 返回 (注册的 tool 数, 注册的 http 路由数)。
pub fn reenable_plugin_capabilities(
    manifest: &PluginManifest,
    registry: &Arc<CapabilityRegistryImpl>,
    scopes: &PluginScopeRegistry,
) -> (usize, usize) {
    // 幂等基线：先收回旧 scope 的全部登记（禁用遗留或重复启用），
    // 再重注册拿全新 guard——避免 by_plugin 索引重复条目。
    scopes.revoke(&manifest.id);
    let tools = register_plugin_capabilities(manifest, registry, scopes);
    let scope = scopes.scope_of(&manifest.id);
    let mut http_routes = 0usize;
    for ep in &manifest.http_endpoints {
        let ok = registry
            .register_http_route_guarded(&manifest.id, ep.clone())
            .map(|(_d, guard)| scope.track(guard))
            .is_ok();
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
    tags: Vec<(String, serde_json::Value)>,
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
    tags: Vec<(String, serde_json::Value)>,
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
            ctx.set(key.as_str(), value);
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
            host_group: None,
            entry: "python server.py".to_string(),
            capabilities: ManifestCapabilities {
                lifecycle_hooks: if declare_domain {
                    vec![LifecycleHook::DomainEvent]
                } else {
                    vec![]
                },
                ..Default::default()
            },
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
            persistent_fields: vec![],
            export_fields: vec![],
            provides: None,
        }
    }

    #[tokio::test]
    async fn domain_event_reaches_declaring_enabled_plugins_only() {
        let invoker = Arc::new(RecordingInvoker {
            hooks: Mutex::new(Vec::new()),
        });
        let mut state = AppState::new();
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
            vec![
                ("session_id".to_string(), json!("t1")),
                ("pipeline_id".to_string(), json!("pipe1")),
            ],
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

#[cfg(test)]
mod external_mcp_schema_gate_tests {
    use super::*;
    use agentos_core::traits::{
        CapabilityRegistry, HostType, ManifestCapabilities, PluginManifest, PluginType,
        ToolCapability,
    };
    use agentos_core::types::ToolCategory;

    fn manifest_with(entry: &str, tool_input_schema: Option<serde_json::Value>) -> PluginManifest {
        PluginManifest {
            id: "p_gate".to_string(),
            name: "p_gate".to_string(),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Tool,
            pipeline_role: None,
            language: "external".to_string(),
            host_type: HostType::Sidecar,
            host_group: None,
            entry: entry.to_string(),
            capabilities: ManifestCapabilities {
                tools: vec![ToolCapability {
                    name: "ext_tool".to_string(),
                    description: Some("desc".to_string()),
                    input_schema: tool_input_schema,
                    output_schema: None,
                    smoke: None,
                    category: Some(ToolCategory::Search),
                    ui: None,
                    render: None,
                }],
                ..Default::default()
            },
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
            persistent_fields: vec![],
            export_fields: vec![],
            provides: None,
        }
    }

    #[test]
    fn external_mcp_tool_without_input_schema_rejected() {
        // external MCP 工具缺 input_schema → 拒注册。manifest 声明是 LLM 工具面
        // input_schema 的唯一真值源（G2 只比对不回填），缺声明即零参数工具、
        // 调用必因缺参被服务端校验拒绝。
        let m = manifest_with("mcp:external", None);
        let registry = Arc::new(CapabilityRegistryImpl::new());
        let scopes = agentos_plugin_loader::PluginScopeRegistry::new();
        let n = register_plugin_capabilities(&m, &registry, &scopes);
        assert_eq!(n, 0, "external MCP 缺 schema 必须拒注册");
        assert!(
            registry.list_tools().iter().all(|t| t.plugin_id != m.id),
            "被拒工具不得出现在能力注册表"
        );
    }

    #[test]
    fn external_mcp_tool_with_input_schema_registered() {
        let m = manifest_with(
            "mcp:external",
            Some(serde_json::json!({
                "type": "object",
                "properties": {"mode": {"type": "string"}},
                "required": ["mode"],
            })),
        );
        let registry = Arc::new(CapabilityRegistryImpl::new());
        let scopes = agentos_plugin_loader::PluginScopeRegistry::new();
        let n = register_plugin_capabilities(&m, &registry, &scopes);
        assert_eq!(n, 1, "带 schema 的 external MCP 工具正常注册");
        let tools = registry.list_tools();
        let t = tools.iter().find(|t| t.plugin_id == m.id).expect("应注册");
        assert_eq!(t.input_schema["required"][0], "mode");
    }

    #[test]
    fn sidecar_builtin_tool_without_input_schema_still_registered() {
        // 内置 sidecar 哨兵（http.handle / *.status / widget_demo 演示工具）零参
        // 合法——维持 {} 补注册（K9 既有行为），不被新规则误伤
        let mut m2 = manifest_with("plugin:main", None);
        m2.plugin_type = PluginType::System;
        m2.language = "python".to_string();
        m2.capabilities.tools[0].name = "http.handle".to_string();
        let registry = Arc::new(CapabilityRegistryImpl::new());
        let scopes = agentos_plugin_loader::PluginScopeRegistry::new();
        let n = register_plugin_capabilities(&m2, &registry, &scopes);
        assert_eq!(n, 1, "内置工具缺 schema 仍补注册");
        let tools = registry.list_tools();
        let t = tools.iter().find(|t| t.plugin_id == m2.id).expect("应注册");
        assert!(t.input_schema.is_object() && t.input_schema.as_object().unwrap().is_empty());
    }
}
