// @feature: FP-0.2.一 插件协议 | @vision: V3 可嵌入 | @audit: T5#16 | @ci: rust-test
//! P6 命名治理（ADR 附录 D②）：PluginManifest.invoke_entry 字段 serde 测试（TDD RED）。
//!
//! invoke_entry 是 pipeline/system 等**非 tool 插件**的 MCP 入口方法名
//! （如 "llm_core.execute"），与"给 LLM 的真工具"分离。
//! tool 类型插件继续用 capabilities.tools[]，不用 invoke_entry。
//!
//! 设计依据：ADR 附录 D.4 改动 ②/③、D.5（一次原子提交）。

use agentos_core::traits::{HostType, PluginManifest, PluginType};

/// 声明了 invoke_entry 的 manifest 应正确反序列化。
#[test]
fn test_manifest_deserializes_invoke_entry() {
    let json = r#"{
        "id": "pipeline_llm_core",
        "name": "LLM Core",
        "version": "1.0.0",
        "plugin_type": "pipeline",
        "language": "python",
        "host_type": "sidecar",
        "entry": "python server.py",
        "capabilities": {},
        "invoke_entry": "llm_core.execute"
    }"#;

    let manifest: PluginManifest = serde_json::from_str(json).expect("manifest must parse");
    assert_eq!(
        manifest.invoke_entry.as_deref(),
        Some("llm_core.execute"),
        "invoke_entry must deserialize from manifest"
    );
}

/// 未声明 invoke_entry 的 manifest 默认 None（向后兼容 tool 类型 + 旧 manifest）。
#[test]
fn test_manifest_without_invoke_entry_defaults_none() {
    let json = r#"{
        "id": "bash_execute",
        "name": "Bash Execute",
        "version": "1.0.0",
        "plugin_type": "tool",
        "language": "python",
        "host_type": "sidecar",
        "entry": "python server.py",
        "capabilities": {
            "tools": [{"name": "bash_execute"}]
        }
    }"#;

    let manifest: PluginManifest = serde_json::from_str(json).expect("manifest must parse");
    assert!(
        manifest.invoke_entry.is_none(),
        "missing invoke_entry must default to None"
    );
}

/// invoke_entry 为 None 时序列化应省略该字段（保持 wire 格式干净）。
#[test]
fn test_none_invoke_entry_omitted_in_serialization() {
    let manifest = PluginManifest {
        id: "p".to_string(),
        name: "P".to_string(),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::Pipeline,
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
        invoke_entry: None,
        persistent_fields: vec![],
    };

    let serialized = serde_json::to_string(&manifest).expect("serialize");
    assert!(
        !serialized.contains("invoke_entry"),
        "None invoke_entry should be omitted, got: {serialized}"
    );
}

/// P6：config_refs 字段已删除——2026-08-18 契约定型改为 `deny_unknown_fields`
/// 拒绝，残留 config_refs 不再被静默忽略（真实语料确认无插件残留，拒绝不破坏启动）。
#[test]
fn test_config_refs_field_removed() {
    // 旧 manifest 残留 config_refs 字段——现在拒绝
    let json = r#"{
        "id": "legacy",
        "name": "Legacy",
        "version": "1.0.0",
        "plugin_type": "system",
        "language": "python",
        "host_type": "sidecar",
        "entry": "python server.py",
        "capabilities": {},
        "config_refs": ["models"]
    }"#;

    let err = serde_json::from_str::<PluginManifest>(json)
        .expect_err("残留未知字段 config_refs 必须拒绝，不再静默忽略");
    let msg = format!("{err:?}");
    assert!(
        msg.contains("config_refs"),
        "错误应指明被拒绝的未知字段: {msg}"
    );
}
