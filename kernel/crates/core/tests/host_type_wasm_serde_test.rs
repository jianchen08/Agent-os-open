//! task_11 N1：HostType::Wasm 变体 + PluginManifest native/wasm 字段 serde 测试。
//!
//! 验证：
//! - `host_type: "wasm"` 反序列化为 `HostType::Wasm`；
//! - manifest 的 `native`/`wasm` 字段正确解析；
//! - 未声明 native/wasm 的 manifest 向后兼容（默认 None）。
//!
//! 设计依据：《原生与WASM插件执行器实现计划》§2.1。

use agentos_core::traits::{
    HostType, NativeArtifact, PluginManifest, PluginType, WasmArtifact,
};

/// host_type: "wasm" 应反序列化为 HostType::Wasm。
#[test]
fn test_host_type_wasm_deserialize() {
    let json = r#"{
        "id": "wasm_review",
        "name": "Wasm Review",
        "version": "1.0.0",
        "plugin_type": "pipeline",
        "language": "rust",
        "host_type": "wasm",
        "entry": "",
        "capabilities": {}
    }"#;
    let manifest: PluginManifest = serde_json::from_str(json).expect("manifest must parse");
    assert_eq!(manifest.host_type, HostType::Wasm);
}

/// HostType::Wasm 序列化为 "wasm"（snake_case）。
#[test]
fn test_host_type_wasm_serialize() {
    let s = serde_json::to_string(&HostType::Wasm).expect("serialize");
    assert_eq!(s, "\"wasm\"");
}

/// 带 native 字段的 manifest（HostType::InProcess）应正确解析 artifact。
#[test]
fn test_manifest_native_artifact_deserialize() {
    let json = r#"{
        "id": "my_fast_filter",
        "name": "Fast Filter",
        "version": "1.0.0",
        "plugin_type": "pipeline",
        "pipeline_role": "input",
        "language": "rust",
        "host_type": "in_process",
        "entry": "",
        "capabilities": {},
        "invoke_entry": "plugin_execute",
        "native": { "artifact": "libmy_fast_filter.so" }
    }"#;
    let manifest: PluginManifest = serde_json::from_str(json).expect("manifest must parse");
    assert_eq!(manifest.host_type, HostType::InProcess);
    let native = manifest.native.expect("native field must be present");
    assert_eq!(native.artifact, "libmy_fast_filter.so");
}

/// 带 wasm 字段的 manifest 应正确解析 artifact / wit_interface / granted_capabilities。
#[test]
fn test_manifest_wasm_artifact_deserialize() {
    let json = r#"{
        "id": "wasm_review",
        "name": "Wasm Review",
        "version": "1.0.0",
        "plugin_type": "pipeline",
        "pipeline_role": "output",
        "language": "rust",
        "host_type": "wasm",
        "entry": "",
        "capabilities": {},
        "invoke_entry": "execute",
        "wasm": {
            "artifact": "wasm_review.wasm",
            "wit_interface": "plugin.wit",
            "granted_capabilities": ["host.log", "host.record_metric"]
        }
    }"#;
    let manifest: PluginManifest = serde_json::from_str(json).expect("manifest must parse");
    let wasm = manifest.wasm.expect("wasm field must be present");
    assert_eq!(wasm.artifact, "wasm_review.wasm");
    assert_eq!(wasm.wit_interface.as_deref(), Some("plugin.wit"));
    assert_eq!(
        wasm.granted_capabilities,
        vec!["host.log".to_string(), "host.record_metric".to_string()]
    );
}

/// 未声明 native/wasm 的 manifest 向后兼容（默认 None）。
#[test]
fn test_manifest_without_native_wasm_defaults_none() {
    let json = r#"{
        "id": "legacy",
        "name": "Legacy",
        "version": "1.0.0",
        "plugin_type": "tool",
        "language": "python",
        "host_type": "sidecar",
        "entry": "python server.py",
        "capabilities": {}
    }"#;
    let manifest: PluginManifest = serde_json::from_str(json).expect("manifest must parse");
    assert!(manifest.native.is_none(), "native must default to None");
    assert!(manifest.wasm.is_none(), "wasm must default to None");
}

/// NativeArtifact / WasmArtifact 独立 serde 往返。
#[test]
fn test_native_and_wasm_artifact_roundtrip() {
    let native = NativeArtifact {
        artifact: "my.dll".to_string(),
    };
    let s = serde_json::to_string(&native).unwrap();
    let back: NativeArtifact = serde_json::from_str(&s).unwrap();
    assert_eq!(back, native);

    let wasm = WasmArtifact {
        artifact: "p.wasm".to_string(),
        wit_interface: Some("plugin.wit".to_string()),
        granted_capabilities: vec!["host.log".to_string()],
    };
    let s = serde_json::to_string(&wasm).unwrap();
    let back: WasmArtifact = serde_json::from_str(&s).unwrap();
    assert_eq!(back, wasm);
}

/// granted_capabilities 为空时序列化应省略（wire 格式干净）。
#[test]
fn test_wasm_artifact_empty_granted_capabilities_omitted() {
    let wasm = WasmArtifact {
        artifact: "p.wasm".to_string(),
        wit_interface: None,
        granted_capabilities: vec![],
    };
    let s = serde_json::to_string(&wasm).unwrap();
    assert!(
        !s.contains("granted_capabilities"),
        "empty granted_capabilities should be omitted, got: {s}"
    );
    assert!(
        !s.contains("wit_interface"),
        "None wit_interface should be omitted, got: {s}"
    );
}

/// 借用现有 manifest 构造（用 Programmatic 构造验证字段可达）。
#[test]
fn test_manifest_programmatic_construction_with_native_wasm() {
    let manifest = PluginManifest {
        id: "p".to_string(),
        name: "P".to_string(),
        version: "1.0.0".to_string(),
        plugin_type: PluginType::Pipeline,
        pipeline_role: None,
        language: "rust".to_string(),
        host_type: HostType::Wasm,
        entry: String::new(),
        capabilities: Default::default(),
        dependencies: vec![],
        permissions: Default::default(),
        error_policy: Default::default(),
        priority: 100,
        mcp: None,
        native: None,
        wasm: Some(WasmArtifact {
            artifact: "p.wasm".to_string(),
            wit_interface: None,
            granted_capabilities: vec!["host.log".to_string()],
        }),
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
    };
    assert_eq!(manifest.host_type, HostType::Wasm);
    assert!(manifest.native.is_none());
    assert!(manifest.wasm.is_some());
}
