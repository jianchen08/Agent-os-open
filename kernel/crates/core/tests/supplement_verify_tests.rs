//! 补充验证测试
//!
//! 填补 contract_tests.rs 中的验证缺口：
//! - 场景3：Composite × Sidecar 组合缺失
//! - 场景5：MessageRecord/TraceEntry/BlobRecord 序列化往返（serialize → deserialize → 比较）
//! - 场景10：CompositeStep 序列化需验证 outputs 字段存在

use std::collections::HashMap;

use serde_json::json;

use agentos_core::traits::*;
use agentos_core::types::*;

// ── 场景3补充：Composite × Sidecar 双路径组合 ──────────────────

#[test]
fn test_composite_plugin_sidecar() {
    let manifest = make_test_manifest(HostType::Sidecar, PluginType::Composite, None);
    assert_eq!(manifest.host_type, HostType::Sidecar);
    assert_eq!(manifest.plugin_type, PluginType::Composite);
}

/// 验证所有 3×2=6 种 PluginType×HostType 组合均可创建 manifest
#[test]
fn test_all_plugin_type_host_type_combinations() {
    let plugin_types = [
        PluginType::Pipeline,
        PluginType::Tool,
        PluginType::Composite,
    ];
    let host_types = [HostType::InProcess, HostType::Sidecar];

    for pt in &plugin_types {
        for ht in &host_types {
            let manifest = make_test_manifest(ht.clone(), pt.clone(), None);
            assert_eq!(manifest.plugin_type, *pt);
            assert_eq!(manifest.host_type, *ht);
        }
    }
}

fn make_test_manifest(
    host_type: HostType,
    plugin_type: PluginType,
    requires_content: Option<u32>,
) -> PluginManifest {
    PluginManifest {
        id: "test_plugin".to_string(),
        name: "Test Plugin".to_string(),
        version: "1.0.0".to_string(),
        plugin_type,
        pipeline_role: None,
        language: "rust".to_string(),
        host_type,
        entry: "test_plugin".to_string(),
        capabilities: ManifestCapabilities::default(),
        dependencies: vec![],
        permissions: ManifestPermissions::default(),
        error_policy: ErrorPolicy::default(),
        priority: 100,
        mcp: None,
        requires_content,
        config_refs: vec![],
        config_files: vec![],
        ui_schema: None,
    }
}

// ── 场景5补充：MessageRecord/TraceEntry/BlobRecord 序列化往返 ──

#[test]
fn test_message_record_roundtrip() {
    let original = MessageRecord {
        message_id: "msg_001".to_string(),
        run_id: "run_001".to_string(),
        branch_id: "main".to_string(),
        seq_in_branch: 1,
        role: "user".to_string(),
        blob_id: Some("blob_001".to_string()),
        content_preview: Some("Hello".to_string()),
        created_at: "2026-07-14T00:00:00Z".to_string(),
    };
    let json_str = serde_json::to_string(&original).unwrap();
    let deserialized: MessageRecord = serde_json::from_str(&json_str).unwrap();
    assert_eq!(deserialized.message_id, original.message_id);
    assert_eq!(deserialized.run_id, original.run_id);
    assert_eq!(deserialized.branch_id, original.branch_id);
    assert_eq!(deserialized.seq_in_branch, original.seq_in_branch);
    assert_eq!(deserialized.role, original.role);
    assert_eq!(deserialized.blob_id, original.blob_id);
    assert_eq!(deserialized.content_preview, original.content_preview);
    assert_eq!(deserialized.created_at, original.created_at);
}

#[test]
fn test_message_record_roundtrip_no_optional() {
    let original = MessageRecord {
        message_id: "msg_002".to_string(),
        run_id: "run_001".to_string(),
        branch_id: "main".to_string(),
        seq_in_branch: 2,
        role: "assistant".to_string(),
        blob_id: None,
        content_preview: None,
        created_at: "2026-07-14T00:01:00Z".to_string(),
    };
    let json_str = serde_json::to_string(&original).unwrap();
    let deserialized: MessageRecord = serde_json::from_str(&json_str).unwrap();
    assert_eq!(deserialized.message_id, "msg_002");
    assert_eq!(deserialized.blob_id, None);
    assert_eq!(deserialized.content_preview, None);
}

#[test]
fn test_trace_entry_roundtrip() {
    let original = TraceEntry {
        trace_id: "trace_001".to_string(),
        run_id: "run_001".to_string(),
        branch_id: "main".to_string(),
        seq_in_branch: 1,
        plugin_id: "plugin_001".to_string(),
        patch_type: PatchType::StateUpdate,
        patch_data: json!({"key": "value", "num": 42}),
        created_at: "2026-07-14T00:00:00Z".to_string(),
    };
    let json_str = serde_json::to_string(&original).unwrap();
    let deserialized: TraceEntry = serde_json::from_str(&json_str).unwrap();
    assert_eq!(deserialized.trace_id, original.trace_id);
    assert_eq!(deserialized.run_id, original.run_id);
    assert_eq!(deserialized.branch_id, original.branch_id);
    assert_eq!(deserialized.seq_in_branch, original.seq_in_branch);
    assert_eq!(deserialized.plugin_id, original.plugin_id);
    assert_eq!(deserialized.patch_type, original.patch_type);
    assert_eq!(deserialized.patch_data, original.patch_data);
    assert_eq!(deserialized.created_at, original.created_at);
}

#[test]
fn test_trace_entry_roundtrip_all_patch_types() {
    let patch_types = [
        PatchType::StateUpdate,
        PatchType::RouteSignal,
        PatchType::Error,
        PatchType::Lifecycle,
        PatchType::Rollback,
    ];
    for pt in &patch_types {
        let entry = TraceEntry {
            trace_id: "t".to_string(),
            run_id: "r".to_string(),
            branch_id: "b".to_string(),
            seq_in_branch: 0,
            plugin_id: "p".to_string(),
            patch_type: pt.clone(),
            patch_data: json!({}),
            created_at: "2026-01-01T00:00:00Z".to_string(),
        };
        let json_str = serde_json::to_string(&entry).unwrap();
        let deserialized: TraceEntry = serde_json::from_str(&json_str).unwrap();
        assert_eq!(deserialized.patch_type, *pt);
    }
}

#[test]
fn test_blob_record_roundtrip() {
    let original = BlobRecord {
        blob_id: "blob_001".to_string(),
        mime_type: "text/plain".to_string(),
        size_bytes: 100,
        created_at: "2026-07-14T00:00:00Z".to_string(),
    };
    let json_str = serde_json::to_string(&original).unwrap();
    let deserialized: BlobRecord = serde_json::from_str(&json_str).unwrap();
    assert_eq!(deserialized.blob_id, original.blob_id);
    assert_eq!(deserialized.mime_type, original.mime_type);
    assert_eq!(deserialized.size_bytes, original.size_bytes);
    assert_eq!(deserialized.created_at, original.created_at);
}

// ── 场景10补充：CompositeStep 序列化验证 outputs 字段 ──────────

#[test]
fn test_composite_step_serialization_has_outputs() {
    let mut outputs = HashMap::new();
    outputs.insert("context".to_string(), "{{result.data}}".to_string());
    outputs.insert("answer".to_string(), "{{result.content}}".to_string());

    let step = CompositeStep {
        name: "retrieve".to_string(),
        plugin: "knowledge_search".to_string(),
        inputs: json!({"query": "{{state.user_query}}"}),
        outputs,
    };
    let json_str = serde_json::to_string(&step).unwrap();
    // 验证所有四个字段都出现在序列化结果中
    assert!(json_str.contains("name"));
    assert!(json_str.contains("plugin"));
    assert!(json_str.contains("inputs"));
    assert!(json_str.contains("outputs"));
    assert!(json_str.contains("context"));
    assert!(json_str.contains("{{result.data}}"));
    // 往返一致
    let deserialized: CompositeStep = serde_json::from_str(&json_str).unwrap();
    assert_eq!(deserialized.name, "retrieve");
    assert_eq!(deserialized.plugin, "knowledge_search");
    assert_eq!(deserialized.outputs.len(), 2);
    assert_eq!(
        deserialized.outputs.get("context"),
        Some(&"{{result.data}}".to_string())
    );
}

#[test]
fn test_composite_plugin_config_roundtrip() {
    let mut outputs = HashMap::new();
    outputs.insert("answer".to_string(), "{{result.content}}".to_string());

    let config = CompositePluginConfig {
        steps: vec![
            CompositeStep {
                name: "retrieve".to_string(),
                plugin: "knowledge_search".to_string(),
                inputs: json!({"query": "{{state.user_query}}"}),
                outputs: HashMap::new(),
            },
            CompositeStep {
                name: "generate".to_string(),
                plugin: "llm_call".to_string(),
                inputs: json!({"messages": []}),
                outputs,
            },
        ],
    };
    let json_str = serde_json::to_string(&config).unwrap();
    assert!(json_str.contains("steps"));
    assert!(json_str.contains("retrieve"));
    assert!(json_str.contains("generate"));
    // 往返一致
    let deserialized: CompositePluginConfig = serde_json::from_str(&json_str).unwrap();
    assert_eq!(deserialized.steps.len(), 2);
    assert_eq!(deserialized.steps[0].name, "retrieve");
    assert_eq!(deserialized.steps[1].name, "generate");
    assert_eq!(deserialized.steps[1].outputs.len(), 1);
}

// ── 场景1补充：HookContext 完整用户旅程（串联验证） ──────────────

#[test]
fn test_hook_context_full_journey() {
    // 步骤1: 创建空上下文
    let mut ctx = HookContext::new();
    assert!(ctx.tags().is_empty());

    // 步骤2: set(key, value) — 链式调用
    ctx.set("session_id", json!("sess_001"))
        .set("iteration", json!(3))
        .set("state_snapshot", json!({"step": 1}));

    // 步骤3: get(key) 返回 Value
    let val = ctx.get("session_id").unwrap();
    assert_eq!(val, &json!("sess_001"));

    // 步骤4: get_as<T> 类型转换
    let session_id: String = ctx.get_as("session_id").unwrap();
    assert_eq!(session_id, "sess_001");
    let iteration: u32 = ctx.get_as("iteration").unwrap();
    assert_eq!(iteration, 3);

    // 步骤5: tags() 返回全部标签
    let all_tags = ctx.tags();
    assert_eq!(all_tags.len(), 3);

    // 步骤6: 序列化往返一致
    let serialized = serde_json::to_string(&ctx).unwrap();
    let deserialized: HookContext = serde_json::from_str(&serialized).unwrap();
    assert_eq!(
        deserialized.get_as::<String>("session_id"),
        Some("sess_001".to_string())
    );
    assert_eq!(deserialized.get_as::<u32>("iteration"), Some(3));
    assert_eq!(deserialized.tags().len(), 3);
}

// ── 场景9补充：PluginManifest requires_content 有值时反序列化 ──

#[test]
fn test_manifest_requires_content_with_value() {
    let json_str = r#"{
        "id": "memory_read",
        "name": "Memory Read",
        "version": "1.0.0",
        "plugin_type": "pipeline",
        "language": "rust",
        "host_type": "in_process",
        "entry": "memory_read",
        "capabilities": {},
        "dependencies": [],
        "permissions": {},
        "error_policy": "abort",
        "priority": 100,
        "requires_content": 5
    }"#;
    let manifest: PluginManifest = serde_json::from_str(json_str).unwrap();
    assert_eq!(manifest.requires_content, Some(5));
}

// ── 补充场景：错误输入验证 ──────────────────────────────────

#[test]
fn test_hook_context_get_as_type_mismatch_returns_none() {
    let mut ctx = HookContext::new();
    ctx.set("string_val", json!("hello"));
    ctx.set("num_val", json!(42));

    // 类型不匹配：String → u32
    let result: Option<u32> = ctx.get_as("string_val");
    assert!(result.is_none());

    // 类型匹配：u32 → u32 (JSON number → u32)
    let num: u32 = ctx.get_as("num_val").unwrap();
    assert_eq!(num, 42);
}

#[test]
fn test_composite_step_empty_outputs_default() {
    let json_str = r#"{
        "name": "step1",
        "plugin": "plugin1",
        "inputs": {}
    }"#;
    // outputs 字段缺失时应该用 default
    let step: CompositeStep = serde_json::from_str(json_str).unwrap();
    assert_eq!(step.name, "step1");
    assert!(step.outputs.is_empty());
}
