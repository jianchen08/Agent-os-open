// @feature: FP-0.2.一 插件协议 | @vision: V3 可嵌入 | @ci: rust-test
//! 插件生命周期注册逻辑测试（阶段 4.2：plugin_lifecycle.rs 真实 0% → 有测）。
//!
//! 核心契约（ADR 附录D①）：只有 plugin_type==Tool 的插件的 tools 才注册到
//! LLM-facing capability_registry；pipeline/system 的工具不暴露给大模型。
//! 兼顾 source 推导（Sidecar→Mcp / InProcess→Builtin）、批量注册去重、
//! http_endpoints 声明判定。

use std::collections::HashSet;

use agentos_api::plugin_lifecycle::{
    reenable_plugin_capabilities, register_new_plugins, register_plugin_capabilities,
};
use agentos_core::traits::{
    CapabilityRegistry, HostType, HttpEndpoint, ManifestCapabilities, PluginManifest, PluginType,
    ToolCapability,
};
use agentos_core::types::{ToolCategory, ToolSource};
use agentos_plugin_loader::{CapabilityRegistryImpl, PluginScopeRegistry};

fn manifest(plugin_id: &str, plugin_type: PluginType, host_type: HostType) -> PluginManifest {
    PluginManifest {
        id: plugin_id.to_string(),
        name: plugin_id.to_string(),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type,
        pipeline_role: None,
        language: "python".to_string(),
        host_type,
        host_group: None,
        entry: "server.py".to_string(),
        capabilities: ManifestCapabilities::default(),
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

fn tool_cap(name: &str) -> ToolCapability {
    ToolCapability {
        name: name.to_string(),
        description: Some(format!("desc {name}")),
        input_schema: None,
        output_schema: None,
        category: Some(ToolCategory::File),
        ui: None,
        render: None,
        smoke: None,
    }
}

#[test]
fn tool_plugin_registers_its_tools() {
    let mut m = manifest("tool-a", PluginType::Tool, HostType::InProcess);
    let scopes = PluginScopeRegistry::new();
    m.capabilities.tools = vec![tool_cap("read_file"), tool_cap("write_file")];
    let reg = std::sync::Arc::new(CapabilityRegistryImpl::new());
    let n = register_plugin_capabilities(&m, &reg, &scopes);
    assert_eq!(n, 2, "应注册 2 个工具");
    assert!(reg.get_tool("read_file").is_some());
    assert!(reg.get_tool("write_file").is_some());
}

/// D.6 槽位拆分（2026-08-15）：capabilities.tools 声明即注册（不看
/// plugin_type）；services 槽不进 LLM 面。废止原附录D① 类型门控。
#[test]
fn declaration_based_registration_tools_any_type_services_not_registered() {
    let reg = std::sync::Arc::new(CapabilityRegistryImpl::new());
    let scopes = PluginScopeRegistry::new();

    // system 适配器声明 tools（多职能插件）→ 注册（原附录D① 会挡掉）
    let mut adapter = manifest("dsh_adapter", PluginType::System, HostType::Sidecar);
    adapter.capabilities.tools = vec![tool_cap("dsh_read"), tool_cap("dsh_glob")];
    assert_eq!(register_plugin_capabilities(&adapter, &reg, &scopes), 2);
    assert!(reg.get_tool("dsh_read").is_some());
    assert!(reg.get_tool("dsh_glob").is_some());

    // system 服务方法声明在 services 槽 → 不进 LLM 面（语义唯一化的另一半）
    let mut service_host = manifest("monitoring", PluginType::System, HostType::Sidecar);
    service_host.capabilities.services = vec![agentos_core::traits::ServiceCapability {
        name: "monitoring.get_metrics".to_string(),
        description: Some("metrics".to_string()),
        input_schema: None,
        output_schema: None,
    }];
    assert_eq!(
        register_plugin_capabilities(&service_host, &reg, &scopes),
        0
    );
    assert!(
        reg.get_tool("monitoring.get_metrics").is_none(),
        "services 槽是内部服务入口，不注册进 LLM 面"
    );
}

#[test]
fn non_tool_plugin_does_not_register_tools() {
    // D.6 槽位拆分（2026-08-15）：原附录D① "类型门控"废止——声明在
    // capabilities.tools 即注册（不分类型）；内部入口应声明在 services 槽。
    // 本测试保留反向锚点：services 槽的条目不注册（语义唯一化的另一半）。
    let mut m = manifest("pipeline-a", PluginType::Pipeline, HostType::InProcess);
    let scopes = PluginScopeRegistry::new();
    m.capabilities.services = vec![agentos_core::traits::ServiceCapability {
        name: "internal_only".to_string(),
        description: None,
        input_schema: None,
        output_schema: None,
    }];
    let reg = std::sync::Arc::new(CapabilityRegistryImpl::new());
    let n = register_plugin_capabilities(&m, &reg, &scopes);
    assert_eq!(n, 0, "services 槽是内部服务声明，不注册进 LLM 面");
    assert!(reg.get_tool("internal_only").is_none());
}

#[test]
fn sidecar_source_is_mcp_inprocess_is_builtin() {
    let mut sidecar = manifest("tool-sidecar", PluginType::Tool, HostType::Sidecar);
    let scopes = PluginScopeRegistry::new();
    sidecar.capabilities.tools = vec![tool_cap("mcp_tool")];
    let reg = std::sync::Arc::new(CapabilityRegistryImpl::new());
    register_plugin_capabilities(&sidecar, &reg, &scopes);
    assert_eq!(reg.get_tool("mcp_tool").unwrap().source, ToolSource::Mcp);

    let mut native = manifest("tool-native", PluginType::Tool, HostType::InProcess);
    native.capabilities.tools = vec![tool_cap("builtin_tool")];
    let reg2 = std::sync::Arc::new(CapabilityRegistryImpl::new());
    register_plugin_capabilities(&native, &reg2, &scopes);
    assert_eq!(
        reg2.get_tool("builtin_tool").unwrap().source,
        ToolSource::Builtin
    );
}

#[test]
fn register_new_plugins_skips_existing_ids() {
    let mut a = manifest("new-tool", PluginType::Tool, HostType::InProcess);
    let scopes = PluginScopeRegistry::new();
    a.capabilities.tools = vec![tool_cap("t1")];
    let b = manifest("existing-tool", PluginType::Tool, HostType::InProcess);

    let existing: HashSet<String> = ["existing-tool".to_string()].into_iter().collect();
    let reg = std::sync::Arc::new(CapabilityRegistryImpl::new());
    let (ids, tools) = register_new_plugins(&[a, b], &existing, &reg, &scopes);
    assert_eq!(ids, vec!["new-tool".to_string()]);
    assert_eq!(tools, 1, "只注册了 new-tool 的 1 个工具");
    assert!(reg.get_tool("t1").is_some());
}

// ── G1 enable 对称化：disable 清四维 → re-enable 全部回来 ──

fn webhook_ep(plugin_id: &str) -> HttpEndpoint {
    HttpEndpoint {
        route_id: "webhook".to_string(),
        method: "POST".to_string(),
        path: format!("/ext/{plugin_id}/webhook"),
        auth: "none".to_string(),
        handler_capability: "http.handle".to_string(),
        timeout_ms: None,
        max_concurrency: None,
        description: None,
    }
}

#[test]
fn disable_then_reenable_restores_capabilities() {
    let mut m = manifest("re-tool", PluginType::Tool, HostType::Sidecar);
    let scopes = PluginScopeRegistry::new();
    m.capabilities.tools = vec![tool_cap("rt1")];
    m.http_endpoints = vec![webhook_ep("re-tool")];

    let reg = std::sync::Arc::new(CapabilityRegistryImpl::new());
    let (tools, routes) = reenable_plugin_capabilities(&m, &reg, &scopes);
    assert_eq!((tools, routes), (1, 1));
    assert!(reg.get_tool("rt1").is_some());
    assert!(reg
        .find_http_route("/ext/re-tool/webhook", "POST")
        .is_some());

    // 禁用：clear_plugin 四维摘除
    reg.clear_plugin("re-tool");
    assert!(reg.get_tool("rt1").is_none());
    assert!(reg
        .find_http_route("/ext/re-tool/webhook", "POST")
        .is_none());

    // 重新启用：立即全部回来（对称，无需重启）
    let (tools2, routes2) = reenable_plugin_capabilities(&m, &reg, &scopes);
    assert_eq!((tools2, routes2), (1, 1));
    assert!(reg.get_tool("rt1").is_some());
    assert!(reg
        .find_http_route("/ext/re-tool/webhook", "POST")
        .is_some());
}

#[test]
fn reenable_on_already_enabled_is_idempotent() {
    // 启用已启用插件：tools 覆盖同名，http 路由同 path+method 冲突忽略——注册表不膨胀
    let mut m = manifest("dup-tool", PluginType::Tool, HostType::Sidecar);
    let scopes = PluginScopeRegistry::new();
    m.capabilities.tools = vec![tool_cap("dt1")];
    m.http_endpoints = vec![webhook_ep("dup-tool")];

    let reg = std::sync::Arc::new(CapabilityRegistryImpl::new());
    reenable_plugin_capabilities(&m, &reg, &scopes);
    reenable_plugin_capabilities(&m, &reg, &scopes);
    assert_eq!(reg.list_http_routes().len(), 1, "同 path+method 只留一条");
    assert!(reg.get_tool("dt1").is_some());
}

// ── M1：scope 结构性收回（P2 验收：disable 后零残留——四维 + broadcaster 绑定）──

#[test]
fn m1_scope_revoke_cleans_registry_and_widget_bindings() {
    use agentos_api::metrics::{remove_plugin_bindings, WidgetBinding};
    use agentos_plugin_loader::PluginScopeRegistry;
    use std::sync::Arc;

    let mut m = manifest("m1-tool", PluginType::Tool, HostType::Sidecar);
    m.capabilities.tools = vec![tool_cap("mt1")];
    m.http_endpoints = vec![webhook_ep("m1-tool")];

    let reg = Arc::new(CapabilityRegistryImpl::new());
    let scopes = Arc::new(PluginScopeRegistry::new());

    // 启用 = guarded 注册（入 scope）
    let (tools, routes) = reenable_plugin_capabilities(&m, &reg, &scopes);
    assert_eq!((tools, routes), (1, 1));

    // broadcaster 绑定维度：模拟启动装配挂 scope guard（与 agentos-kernel 同构）。
    let bindings = Arc::new(parking_lot::RwLock::new(vec![WidgetBinding {
        widget_id: "w1".to_string(),
        plugin_id: "m1-tool".to_string(),
        metric: "m.x".to_string(),
        interval: std::time::Duration::from_secs(1),
        scope: agentos_api::metrics::BindingScope::Broadcast,
        owner_plugin_id: "m1-tool".to_string(),
    }]));
    {
        let shared = Arc::clone(&bindings);
        let owner = "m1-tool".to_string();
        scopes
            .scope_of("m1-tool")
            .track(agentos_core::traits::RegistrationGuard::new(move || {
                remove_plugin_bindings(&shared, &owner);
            }));
    }

    // 禁用语义（对齐 routes.rs disable 分支）：scope revoke + clear_plugin 兜底。
    scopes.revoke("m1-tool");
    reg.clear_plugin("m1-tool");

    // 残留扫描：可查询维（tool / http 路由）+ broadcaster 绑定断言为空。
    // （resource / route-signal 的查询面已随死代码删除——plugin-loader registry
    // 不再暴露 list_resources/has_route_signal；收回路径经 scope revoke +
    // clear_plugin 结构性保证，无可查询残留面。）
    assert!(reg.get_tool("mt1").is_none(), "tool 残留");
    assert!(
        reg.find_http_route("/ext/m1-tool/webhook", "POST")
            .is_none(),
        "http 路由残留"
    );
    assert!(bindings.read().is_empty(), "widget 绑定残留");
    assert!(scopes.is_empty(), "scope 表残留");
}

#[test]
fn m1_scoped_reenable_is_idempotent_no_index_bloat() {
    // scope 化路径下重复 reenable：先 revoke 旧 scope 再重注册，by_plugin 索引不重复膨胀。
    use agentos_plugin_loader::PluginScopeRegistry;
    use std::sync::Arc;

    let mut m = manifest("m1-dup", PluginType::Tool, HostType::Sidecar);
    m.capabilities.tools = vec![tool_cap("md1")];
    m.http_endpoints = vec![webhook_ep("m1-dup")];

    let reg = Arc::new(CapabilityRegistryImpl::new());
    let scopes = PluginScopeRegistry::new();
    reenable_plugin_capabilities(&m, &reg, &scopes);
    reenable_plugin_capabilities(&m, &reg, &scopes);

    assert_eq!(reg.list_tools().len(), 1, "同名工具只留一条");
    assert_eq!(reg.list_http_routes().len(), 1, "同 path+method 只留一条");
    // 禁用后零残留（scope 路径）
    scopes.revoke("m1-dup");
    assert!(reg.get_tool("md1").is_none());
    assert!(reg.list_http_routes().is_empty());
}
