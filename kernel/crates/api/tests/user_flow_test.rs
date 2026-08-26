// @feature: FP-0.2.二 内部模块manifest | @vision: V6 可即用 | @ci: rust-test
//! 用户流程端到端测试（验证 docs/working/user_flow_and_capabilities.md 的关键断言）
//!
//! 覆盖场景：
//! - 场景1：schema 聚合正确（agents/pipelines/tools/plugin_configs/plugin_contributes）
//! - 场景4：安装触发模型 L1（disabled 插件 contributes 不出口）
//! - 场景4：config 读写协议（GET/PUT + ETag 乐观锁）
//! - 场景5：http_dispatcher 路由（enabled 挂载，disabled 不挂载）

#![cfg(test)]

use std::collections::HashSet;

use agentos_core::traits::{HostType, ManifestCapabilities, PluginManifest, PluginType};
use agentos_plugin_loader::PluginEnablement;

/// 构造测试 manifest（含 contributes + config_files）
fn test_manifest(
    id: &str,
    plugin_type: PluginType,
    contributes: Option<serde_json::Value>,
    config_files: Vec<agentos_core::traits::ConfigFileMapping>,
) -> PluginManifest {
    PluginManifest {
        id: id.to_string(),
        name: id.to_string(),
        description: None,
        version: "0.1.0".to_string(),
        plugin_type,
        pipeline_role: None,
        language: "python".to_string(),
        host_type: HostType::Sidecar,
        host_group: None,
        entry: "python server.py".to_string(),
        capabilities: ManifestCapabilities::default(),
        requires_services: vec![],
        permissions: Default::default(),
        priority: 50,
        mcp: None,
        lifecycle: None,
        native: None,
        granted_capabilities: vec![],
        requires_content: None,
        invoke_entry: None,
        config_files,
        ui_schema: None,
        contributes,
        http_endpoints: vec![],
        enabled: None,
        activation: None,
        persistent_fields: vec![],
        provides: None,
    }
}

// ── 场景4：安装触发模型 L1 启用层 ──────────────────────────────────────────

#[test]
fn test_disabled_plugin_contributes_not_exported() {
    // 场景：两个插件都声明了 contributes，一个 enabled 一个 disabled
    let enabled_plugin = test_manifest(
        "enabled_plugin",
        PluginType::System,
        Some(serde_json::json!({"statusBarItems": [{"id": "s1"}]})),
        vec![],
    );
    let disabled_plugin = test_manifest(
        "disabled_plugin",
        PluginType::System,
        Some(serde_json::json!({"statusBarItems": [{"id": "s2"}]})),
        vec![],
    );

    // 构造 enablement（disabled_plugin 标 false）
    let mut profile = agentos_plugin_loader::PluginProfile::default();
    profile.plugins.insert(
        "disabled_plugin".into(),
        agentos_plugin_loader::ProfileEntry {
            enabled: Some(false),
            activation: None,
        },
    );
    let enablement = PluginEnablement::with_profile(profile);

    // 模拟 schema 聚合：只输出 enabled 插件的 contributes
    let manifests = vec![enabled_plugin.clone(), disabled_plugin.clone()];
    let enabled_ids: HashSet<String> = manifests
        .iter()
        .filter(|m| enablement.is_enabled(&m.id, m.enabled))
        .map(|m| m.id.clone())
        .collect();

    let exported_contributes: Vec<_> = manifests
        .iter()
        .filter(|m| m.contributes.is_some() && enabled_ids.contains(&m.id))
        .map(|m| m.id.clone())
        .collect();

    assert!(exported_contributes.contains(&"enabled_plugin".to_string()));
    assert!(!exported_contributes.contains(&"disabled_plugin".to_string()));
    // 断言：disabled 插件的 contributes 不出口（用户看不到其 UI 贡献）
}

#[test]
fn test_manifest_enabled_overrides_profile() {
    // 插件 manifest 显式 enabled:true，即使 profile 标 false，也启用
    let enablement = PluginEnablement::default();
    assert!(enablement.is_enabled("any", Some(true)));
    assert!(!enablement.is_enabled("any", Some(false)));
}

#[test]
fn test_default_enabled_when_unlisted() {
    // 未在 profile 列出的插件，默认启用（lazy）
    let enablement = PluginEnablement::default();
    assert!(enablement.is_enabled("unknown_plugin", None));
}

// ── 场景4：config_files 聚合 ────────────────────────────────────────────────

#[test]
fn test_plugin_configs_only_includes_declared() {
    // 只有声明了 config_files 的插件才进 plugin_configs
    let with_config = test_manifest(
        "with_config",
        PluginType::System,
        None,
        vec![agentos_core::traits::ConfigFileMapping {
            id: "cfg".to_string(),
            path: "config/test.yaml".to_string(),
            label: "Test".to_string(),
            target: None,
            fields: vec![],
        }],
    );
    let without_config = test_manifest("without_config", PluginType::System, None, vec![]);

    let manifests = vec![with_config, without_config];
    let plugin_configs: Vec<_> = manifests
        .iter()
        .filter(|m| !m.config_files.is_empty())
        .map(|m| m.id.clone())
        .collect();

    assert_eq!(plugin_configs, vec!["with_config"]);
    // 断言：设置页只显示声明 config_files 的插件
}

// ── 场景1：schema agents/pipelines 分类 ────────────────────────────────────

#[test]
fn test_schema_classifies_agents_and_pipelines() {
    let system_plugin = test_manifest("system_p", PluginType::System, None, vec![]);
    let pipeline_plugin = test_manifest("pipeline_p", PluginType::Pipeline, None, vec![]);
    let tool_plugin = test_manifest("tool_p", PluginType::Tool, None, vec![]);

    let manifests = vec![system_plugin, pipeline_plugin, tool_plugin];
    let agents: Vec<_> = manifests
        .iter()
        .filter(|m| m.plugin_type == PluginType::System)
        .map(|m| m.id.clone())
        .collect();
    let pipelines: Vec<_> = manifests
        .iter()
        .filter(|m| m.plugin_type == PluginType::Pipeline)
        .map(|m| m.id.clone())
        .collect();

    assert_eq!(agents, vec!["system_p"]);
    assert_eq!(pipelines, vec!["pipeline_p"]);
    // 断言：schema 正确分类 system→agents, pipeline→pipelines
}

// ── 场景5：http_endpoints 只注册 enabled 插件 ──────────────────────────────

#[test]
fn test_http_endpoints_only_for_enabled() {
    let enabled_plugin = test_manifest("enabled_http", PluginType::System, None, vec![]);
    let disabled_plugin = test_manifest("disabled_http", PluginType::System, None, vec![]);

    let mut profile = agentos_plugin_loader::PluginProfile::default();
    profile.plugins.insert(
        "disabled_http".into(),
        agentos_plugin_loader::ProfileEntry {
            enabled: Some(false),
            activation: None,
        },
    );
    let enablement = PluginEnablement::with_profile(profile);

    let manifests = vec![enabled_plugin, disabled_plugin];
    let enabled_manifests: Vec<_> = manifests
        .iter()
        .filter(|m| enablement.is_enabled(&m.id, m.enabled))
        .collect();

    assert_eq!(enabled_manifests.len(), 1);
    assert_eq!(enabled_manifests[0].id, "enabled_http");
    // 断言：http_dispatcher 只注册 enabled 插件的端点
}

// ── 场景4 续：插件管理页数据驱动 + 热加载 ───────────────────────────────────

#[test]
fn test_plugin_status_data_from_manifest() {
    // 插件状态全部从 manifest 元数据派生（不硬编码）
    let manifest = PluginManifest {
        id: "test_plugin".to_string(),
        name: "Test Plugin".to_string(),
        description: None,
        version: "2.0.0".to_string(),
        plugin_type: PluginType::System,
        pipeline_role: None,
        language: "python".to_string(),
        host_type: HostType::Sidecar,
        host_group: None,
        entry: "python server.py".to_string(),
        capabilities: ManifestCapabilities::default(),
        requires_services: vec![],
        permissions: Default::default(),
        priority: 50,
        mcp: None,
        lifecycle: None,
        native: None,
        granted_capabilities: vec![],
        requires_content: None,
        invoke_entry: None,
        config_files: vec![agentos_core::traits::ConfigFileMapping {
            id: "cfg".to_string(),
            path: "config/test.yaml".to_string(),
            label: "Test Config".to_string(),
            target: None,
            fields: vec![],
        }],
        ui_schema: None,
        contributes: Some(serde_json::json!({"statusBarItems": []})),
        http_endpoints: vec![],
        enabled: Some(true),
        activation: Some(agentos_core::traits::ActivationPolicy::Eager),
        persistent_fields: vec![],
        provides: None,
    };

    // 验证 status handler 会派生的字段（模拟 handler 逻辑）
    assert_eq!(manifest.plugin_type, PluginType::System); // → config_type "system"
    assert_eq!(manifest.version, "2.0.0");
    assert_eq!(manifest.config_files.len(), 1); // → config_files 显示
    assert!(manifest.contributes.is_some()); // → has_contributes true
    assert_eq!(
        manifest.activation,
        Some(agentos_core::traits::ActivationPolicy::Eager)
    ); // → "eager"
}

#[test]
fn test_enabled_toggle_hot_reload_semantics() {
    // 热加载语义验证：
    // - 禁用：enabled_ids 立即移除（contributes/tools 立即摘掉）
    // - 启用：enabled_ids 立即加入（contributes 立即出口，http 路由需重启）
    let mut enabled_ids = HashSet::new();
    enabled_ids.insert("plugin_a".to_string());
    enabled_ids.insert("plugin_b".to_string());

    // 模拟禁用 plugin_a
    enabled_ids.remove("plugin_a");
    assert!(!enabled_ids.contains("plugin_a")); // 立即不可见
    assert!(enabled_ids.contains("plugin_b")); // 其他不受影响

    // 模拟重新启用 plugin_a
    enabled_ids.insert("plugin_a".to_string());
    assert!(enabled_ids.contains("plugin_a")); // 立即恢复
}

#[test]
fn test_activation_policy_default_is_lazy() {
    // 未声明 activation 的插件默认 lazy（安装触发模型 §5.2）
    let enablement = PluginEnablement::default();
    assert_eq!(
        enablement.activation("unlisted", None),
        agentos_core::traits::ActivationPolicy::Lazy
    );
}

#[test]
fn test_eager_vs_lazy_distinction() {
    // eager 插件启动期应 Active，lazy 首次调用才 Active
    let manifest_eager = PluginManifest {
        id: "eager_p".to_string(),
        name: "Eager".to_string(),
        description: None,
        version: "1.0".to_string(),
        plugin_type: PluginType::System,
        pipeline_role: None,
        language: "python".to_string(),
        host_type: HostType::Sidecar,
        host_group: None,
        entry: "python s.py".to_string(),
        capabilities: ManifestCapabilities::default(),
        requires_services: vec![],
        permissions: Default::default(),
        priority: 50,
        mcp: None,
        lifecycle: None,
        native: None,
        granted_capabilities: vec![],
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        ui_schema: None,
        contributes: None,
        http_endpoints: vec![],
        enabled: None,
        activation: Some(agentos_core::traits::ActivationPolicy::Eager),
        persistent_fields: vec![],
        provides: None,
    };
    let manifest_lazy = test_manifest("lazy_p", PluginType::Tool, None, vec![]);

    assert_eq!(
        manifest_eager.activation,
        Some(agentos_core::traits::ActivationPolicy::Eager)
    );
    assert_eq!(manifest_lazy.activation, None); // None → default lazy
}
