// @feature: FP-0.2.一 插件协议 | @vision: V3 可嵌入 | @audit: T5#16 | @ci: rust-test
//! P1-1/P6: PluginManifest.config_files 字段 serde 测试。
//!
//! 设计依据：ADR §4.2 / §4.3（config_files: id/path/label 三要素）。
//! P6 更新：config_refs 字段已删除（被 config_files 取代），invoke_entry 字段新增
//! （ADR 附录 D②）。本文件验证 config_files 映射 + invoke_entry 并存的 serde 行为。

use agentos_core::traits::{HostType, PluginManifest, PluginType};

/// 反序列化含 config_files 的 manifest——应解析出每个映射项的 id/path/label。
#[test]
fn test_manifest_deserializes_config_files() {
    let json = r#"{
        "id": "llm_service",
        "name": "LLM Service",
        "version": "1.0.0",
        "plugin_type": "system",
        "language": "python",
        "host_type": "sidecar",
        "entry": "python server.py",
        "capabilities": {},
        "config_files": [
            {"id": "llm", "path": "config/models/llm.yaml", "label": "LLM 模型配置"},
            {"id": "embedding", "path": "config/models/embedding.yaml", "label": "向量模型配置"}
        ]
    }"#;

    let manifest: PluginManifest = serde_json::from_str(json).expect("manifest must parse");

    assert_eq!(manifest.config_files.len(), 2);
    assert_eq!(manifest.config_files[0].id, "llm");
    assert_eq!(manifest.config_files[0].path, "config/models/llm.yaml");
    assert_eq!(manifest.config_files[0].label, "LLM 模型配置");
    assert_eq!(manifest.config_files[1].id, "embedding");
    assert_eq!(
        manifest.config_files[1].path,
        "config/models/embedding.yaml"
    );
}

/// 未声明 config_files 的 manifest 应默认空；但残留的 `config_refs` 已是非契约
/// 未知字段（P6 删除后）——2026-08-18 契约定型改为 `deny_unknown_fields` 拒绝，
/// 不再"声明了却静默忽略"（真实语料扫描确认无插件残留 config_refs，拒绝不破坏启动）。
#[test]
fn test_manifest_without_config_files_defaults_empty() {
    let json = r#"{
        "id": "memory",
        "name": "Memory",
        "version": "1.0.0",
        "plugin_type": "system",
        "language": "python",
        "host_type": "sidecar",
        "entry": "python server.py",
        "capabilities": {},
        "config_refs": ["memory_storage"]
    }"#;

    let err = serde_json::from_str::<PluginManifest>(json)
        .expect_err("残留未知字段 config_refs 必须拒绝，不再静默忽略");
    let msg = format!("{err:?}");
    assert!(
        msg.contains("config_refs"),
        "错误应指明被拒绝的未知字段: {msg}"
    );
}

/// P6：config_files + invoke_entry 可并存（pipeline 插件既有配置映射又有 MCP 入口）。
#[test]
fn test_config_files_and_invoke_entry_coexist() {
    let json = r#"{
        "id": "dual",
        "name": "Dual",
        "version": "1.0.0",
        "plugin_type": "pipeline",
        "language": "python",
        "host_type": "sidecar",
        "entry": "python server.py",
        "capabilities": {},
        "invoke_entry": "dual.execute",
        "config_files": [
            {"id": "llm", "path": "config/models/llm.yaml", "label": "LLM"}
        ]
    }"#;

    let manifest: PluginManifest = serde_json::from_str(json).expect("manifest must parse");

    assert_eq!(
        manifest.invoke_entry.as_deref(),
        Some("dual.execute"),
        "invoke_entry must parse alongside config_files"
    );
    assert_eq!(manifest.config_files.len(), 1);
}

/// 序列化时 config_files 为空应被省略（保持向后兼容的 wire 格式）。
#[test]
fn test_empty_config_files_omitted_in_serialization() {
    let manifest = PluginManifest {
        id: "p".to_string(),
        name: "P".to_string(),
        description: None,
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
    };

    let serialized = serde_json::to_string(&manifest).expect("serialize");
    assert!(
        !serialized.contains("config_files"),
        "empty config_files should be omitted, got: {serialized}"
    );
    assert!(
        !serialized.contains("invoke_entry"),
        "None invoke_entry should be omitted, got: {serialized}"
    );
}
