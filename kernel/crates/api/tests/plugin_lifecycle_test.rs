// @feature: FP-0.2.一 插件协议 | @vision: V3 可嵌入 | @ci: rust-test
//! 插件生命周期注册逻辑测试（阶段 4.2：plugin_lifecycle.rs 真实 0% → 有测）。
//!
//! 核心契约（ADR 附录D①）：只有 plugin_type==Tool 的插件的 tools 才注册到
//! LLM-facing capability_registry；pipeline/system 的工具不暴露给大模型。
//! 兼顾 source 推导（Sidecar→Mcp / InProcess→Builtin）、批量注册去重、
//! http_endpoints 声明判定。

use std::collections::HashSet;

use agentos_api::plugin_lifecycle::{
    has_http_endpoints, register_new_plugins, register_plugin_capabilities,
};
use agentos_core::traits::{
    CapabilityRegistry, HostType, HttpEndpoint, ManifestCapabilities, PluginManifest,
    PluginType, ToolCapability,
};
use agentos_core::types::{ToolCategory, ToolSource};
use agentos_plugin_loader::CapabilityRegistryImpl;

fn manifest(plugin_id: &str, plugin_type: PluginType, host_type: HostType) -> PluginManifest {
    PluginManifest {
        id: plugin_id.to_string(),
        name: plugin_id.to_string(),
        version: "1.0.0".to_string(),
        plugin_type,
        pipeline_role: None,
        language: "python".to_string(),
        host_type,
        entry: "server.py".to_string(),
        capabilities: ManifestCapabilities::default(),
        dependencies: vec![],
        permissions: Default::default(),
        error_policy: Default::default(),
        priority: 100,
        mcp: None,
        native: None,
        wasm: None,
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

fn tool_cap(name: &str) -> ToolCapability {
    ToolCapability {
        name: name.to_string(),
        description: Some(format!("desc {name}")),
        input_schema: None,
        output_schema: None,
        category: Some(ToolCategory::File),
        ui: None,
    }
}

#[test]
fn tool_plugin_registers_its_tools() {
    let mut m = manifest("tool-a", PluginType::Tool, HostType::InProcess);
    m.capabilities.tools = vec![tool_cap("read_file"), tool_cap("write_file")];
    let reg = CapabilityRegistryImpl::new();
    let n = register_plugin_capabilities(&m, &reg);
    assert_eq!(n, 2, "应注册 2 个工具");
    assert!(reg.get_tool("read_file").is_some());
    assert!(reg.get_tool("write_file").is_some());
}

#[test]
fn non_tool_plugin_does_not_register_tools() {
    // pipeline/system 插件即便声明了 tools，也不注册到 LLM-facing registry（ADR D①）
    let mut m = manifest("pipeline-a", PluginType::Pipeline, HostType::InProcess);
    m.capabilities.tools = vec![tool_cap("internal_only")];
    let reg = CapabilityRegistryImpl::new();
    let n = register_plugin_capabilities(&m, &reg);
    assert_eq!(n, 0, "非 Tool 插件不应注册工具");
    assert!(reg.get_tool("internal_only").is_none());
}

#[test]
fn sidecar_source_is_mcp_inprocess_is_builtin() {
    let mut sidecar = manifest("tool-sidecar", PluginType::Tool, HostType::Sidecar);
    sidecar.capabilities.tools = vec![tool_cap("mcp_tool")];
    let reg = CapabilityRegistryImpl::new();
    register_plugin_capabilities(&sidecar, &reg);
    assert_eq!(
        reg.get_tool("mcp_tool").unwrap().source,
        ToolSource::Mcp
    );

    let mut native = manifest("tool-native", PluginType::Tool, HostType::InProcess);
    native.capabilities.tools = vec![tool_cap("builtin_tool")];
    let reg2 = CapabilityRegistryImpl::new();
    register_plugin_capabilities(&native, &reg2);
    assert_eq!(
        reg2.get_tool("builtin_tool").unwrap().source,
        ToolSource::Builtin
    );
}

#[test]
fn register_new_plugins_skips_existing_ids() {
    let mut a = manifest("new-tool", PluginType::Tool, HostType::InProcess);
    a.capabilities.tools = vec![tool_cap("t1")];
    let b = manifest("existing-tool", PluginType::Tool, HostType::InProcess);

    let existing: HashSet<String> = ["existing-tool".to_string()].into_iter().collect();
    let reg = CapabilityRegistryImpl::new();
    let (ids, tools) = register_new_plugins(&[a, b], &existing, &reg);
    assert_eq!(ids, vec!["new-tool".to_string()]);
    assert_eq!(tools, 1, "只注册了 new-tool 的 1 个工具");
    assert!(reg.get_tool("t1").is_some());
}

#[test]
fn has_http_endpoints_detects_declaration() {
    let empty = manifest("p1", PluginType::System, HostType::InProcess);
    assert!(!has_http_endpoints(&empty));

    let mut with_ep = manifest("p2", PluginType::System, HostType::Sidecar);
    with_ep.http_endpoints = vec![HttpEndpoint {
        route_id: "webhook".to_string(),
        method: "POST".to_string(),
        path: "/ext/p2/webhook".to_string(),
        auth: "none".to_string(),
        handler_capability: "http.handle".to_string(),
        timeout_ms: None,
        max_concurrency: None,
        description: None,
    }];
    assert!(has_http_endpoints(&with_ep));
}
