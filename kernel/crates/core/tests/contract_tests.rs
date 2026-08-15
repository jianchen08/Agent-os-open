// @feature: FP-0.2.一 插件协议 | @vision: V3 可嵌入 | @audit: T5#16 | @ci: rust-test
//! 契约定义测试
//!
//! 验证 ADR 修订后的契约类型可编译且字段/方法符合预期。
//! 对应 AC-01 ~ AC-11。

use std::collections::HashMap;
use std::sync::Arc;

use async_trait::async_trait;
use serde_json::json;
use uuid::Uuid;

use agentos_core::traits::*;
use agentos_core::types::*;

// ═══════════════════════════════════════════════════════════════════
// ADR ⑨: HookContext 标签化动态上下文
// ═══════════════════════════════════════════════════════════════════

#[test]
fn test_hook_context_tag_based_set_get() {
    let mut ctx = HookContext::new();
    ctx.set("session_id", json!("sess_001"));
    ctx.set("task_id", json!("task_001"));
    ctx.set("tenant_id", json!("tenant_001"));
    ctx.set("pipeline_id", json!(Uuid::new_v4()));
    ctx.set("iteration", json!(3));
    // ADR ⑤ 新增字段
    ctx.set("branch_id", json!("main"));
    ctx.set("seq_in_branch", json!(3));

    let session_id: String = ctx.get_as("session_id").unwrap();
    assert_eq!(session_id, "sess_001");

    let iteration: u32 = ctx.get_as("iteration").unwrap();
    assert_eq!(iteration, 3);

    let branch_id: String = ctx.get_as("branch_id").unwrap();
    assert_eq!(branch_id, "main");
}

#[test]
fn test_hook_context_missing_key_returns_none() {
    let ctx = HookContext::new();
    assert!(ctx.get("nonexistent").is_none());
    assert!(ctx.get_as::<String>("nonexistent").is_none());
}

#[test]
fn test_hook_context_overwrite() {
    let mut ctx = HookContext::new();
    ctx.set("key", json!("v1"));
    ctx.set("key", json!("v2"));
    assert_eq!(ctx.get_as::<String>("key"), Some("v2".to_string()));
}

#[test]
fn test_hook_context_serialization() {
    let mut ctx = HookContext::new();
    ctx.set("key", json!("value"));
    ctx.set("num", json!(42));
    let serialized = serde_json::to_string(&ctx).unwrap();
    let deserialized: HookContext = serde_json::from_str(&serialized).unwrap();
    assert_eq!(
        deserialized.get_as::<String>("key"),
        Some("value".to_string())
    );
    assert_eq!(deserialized.get_as::<i64>("num"), Some(42));
}

#[test]
fn test_hook_context_tags_accessor() {
    let mut ctx = HookContext::new();
    ctx.set("a", json!(1));
    ctx.set("b", json!(2));
    let tags = ctx.tags();
    assert_eq!(tags.len(), 2);
    assert!(tags.contains_key("a"));
    assert!(tags.contains_key("b"));
}

// ═══════════════════════════════════════════════════════════════════
// ADR ⑥: PluginType 新增 Composite
// ═══════════════════════════════════════════════════════════════════

#[test]
fn test_plugin_type_composite_exists() {
    let composite = PluginType::Composite;
    let serialized = serde_json::to_string(&composite).unwrap();
    assert_eq!(serialized, "\"composite\"");
    let deserialized: PluginType = serde_json::from_str(&serialized).unwrap();
    assert_eq!(deserialized, PluginType::Composite);
}

#[test]
fn test_plugin_type_all_variants() {
    let types = [
        PluginType::Pipeline,
        PluginType::Tool,
        PluginType::System,
        PluginType::Composite,
    ];
    assert_eq!(types.len(), 4);
}

// ═══════════════════════════════════════════════════════════════════
// ADR ⑦: PluginManifest requires_content + ContentLoader
// ═══════════════════════════════════════════════════════════════════

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
        lifecycle: None,
        native: None,
        granted_capabilities: vec![],
        requires_content,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        enabled: None,
        activation: None,
        provides: None,
        persistent_fields: vec![],
    }
}

#[test]
fn test_manifest_requires_content_field() {
    let manifest = make_test_manifest(HostType::InProcess, PluginType::Pipeline, Some(2));
    assert_eq!(manifest.requires_content, Some(2));
}

#[test]
fn test_manifest_requires_content_default_none() {
    let json_str = r#"{
        "id": "test",
        "name": "Test",
        "version": "1.0.0",
        "plugin_type": "pipeline",
        "language": "rust",
        "host_type": "in_process",
        "entry": "test",
        "capabilities": {},
        "dependencies": [],
        "permissions": {},
        "error_policy": "abort",
        "priority": 100
    }"#;
    let manifest: PluginManifest = serde_json::from_str(json_str).unwrap();
    assert!(manifest.requires_content.is_none());
}

// ═══════════════════════════════════════════════════════════════════
// ADR ⑦: ContentLoader + StorageBackend
// ═══════════════════════════════════════════════════════════════════

#[derive(Debug)]
struct MockStorageBackend;

#[async_trait]
impl StorageBackend for MockStorageBackend {
    async fn get_run(&self, _run_id: &str) -> Result<RunRecord, StorageError> {
        Err(StorageError::NotFound("mock".to_string()))
    }
    async fn get_messages_by_pipeline(
        &self,
        _pipeline_id: &str,
        _opts: MessageQueryOpts,
    ) -> Result<Vec<MessageRecord>, StorageError> {
        Ok(vec![])
    }
    async fn get_blob(&self, _blob_id: &str) -> Result<Vec<u8>, StorageError> {
        Ok(vec![1, 2, 3])
    }
    async fn append_trace(&self, _entry: TraceEntry) -> Result<(), StorageError> {
        Ok(())
    }
    async fn create_branch(&self, _branch: Branch) -> Result<(), StorageError> {
        Ok(())
    }
    async fn update_run_status(
        &self,
        _run_id: &str,
        _status: RunStatus,
        _branch: Option<&str>,
        _seq: Option<u32>,
    ) -> Result<(), StorageError> {
        Ok(())
    }
    async fn create_run(
        &self,
        _run_id: &str,
        _config_hash: &str,
        _tenant_id: &str,
    ) -> Result<(), StorageError> {
        Ok(())
    }
    async fn store_blob(&self, _data: &[u8], _mime_type: &str) -> Result<String, StorageError> {
        Ok("mock_blob".to_string())
    }
    async fn create_session(&self, _session: &SessionRecord) -> Result<(), StorageError> {
        Ok(())
    }
    async fn get_session(&self, _thread_id: &str) -> Result<Option<SessionRecord>, StorageError> {
        Ok(None)
    }
    async fn list_sessions(
        &self,
        _filter: SessionListFilter,
    ) -> Result<Vec<SessionRecord>, StorageError> {
        Ok(vec![])
    }
    async fn update_session(&self, _session: &SessionRecord) -> Result<(), StorageError> {
        Ok(())
    }
    async fn delete_session(&self, _thread_id: &str) -> Result<(), StorageError> {
        Ok(())
    }
    async fn link_pipeline_session(
        &self,
        _pipeline_id: &str,
        _thread_id: &str,
        _tenant_id: &str,
    ) -> Result<(), StorageError> {
        Ok(())
    }
    async fn list_pipeline_ids_by_thread(
        &self,
        _thread_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<String>, StorageError> {
        Ok(vec![])
    }
    async fn get_step_traces_by_thread(
        &self,
        _thread_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<TraceEntry>, StorageError> {
        Ok(vec![])
    }

    // ── 域3/4/5 stub（M1）──────────────────────────────────────────
    async fn append_execution_record(&self, _record: &ExecutionRecord) -> Result<(), StorageError> {
        Ok(())
    }
    async fn list_execution_records(
        &self,
        _pipeline_run_id: &str,
        _opts: MessageQueryOpts,
    ) -> Result<Vec<ExecutionRecord>, StorageError> {
        Ok(vec![])
    }
    async fn count_execution_records(&self, _pipeline_run_id: &str) -> Result<u64, StorageError> {
        Ok(0)
    }
    async fn delete_execution_records_by_session(
        &self,
        _pipeline_run_id: &str,
    ) -> Result<u64, StorageError> {
        Ok(0)
    }
    async fn save_run_summary(&self, _summary: &PipelineRunSummary) -> Result<(), StorageError> {
        Ok(())
    }
    async fn get_run_summary(
        &self,
        _run_id: &str,
    ) -> Result<Option<PipelineRunSummary>, StorageError> {
        Ok(None)
    }
    async fn update_run_summary(
        &self,
        _run_id: &str,
        _updates: &serde_json::Value,
    ) -> Result<(), StorageError> {
        Ok(())
    }
    async fn list_run_summaries(
        &self,
        _limit: Option<usize>,
    ) -> Result<Vec<PipelineRunSummary>, StorageError> {
        Ok(vec![])
    }
    async fn create_memory(&self, _memory: &MemoryRecord) -> Result<(), StorageError> {
        Ok(())
    }
    async fn get_memory(&self, _id: &str) -> Result<Option<MemoryRecord>, StorageError> {
        Ok(None)
    }
    async fn list_memory(
        &self,
        _memory_type: Option<&str>,
        _limit: usize,
        _offset: usize,
    ) -> Result<Vec<MemoryRecord>, StorageError> {
        Ok(vec![])
    }
    async fn search_memory(
        &self,
        _query: &str,
        _top_k: usize,
    ) -> Result<Vec<MemoryRecord>, StorageError> {
        Ok(vec![])
    }
    async fn delete_memory(&self, _id: &str) -> Result<bool, StorageError> {
        Ok(false)
    }

    // ── users（0.5.0 最小持久化）：mock 不实现真实用户存储，返回空/未实现
    async fn create_user(
        &self,
        _user: &agentos_core::types::UserRecord,
    ) -> Result<(), StorageError> {
        Ok(())
    }
    async fn get_user_by_id(
        &self,
        _user_id: &str,
    ) -> Result<Option<agentos_core::types::UserRecord>, StorageError> {
        Ok(None)
    }
    async fn get_user_by_username(
        &self,
        _username: &str,
    ) -> Result<Option<agentos_core::types::UserRecord>, StorageError> {
        Ok(None)
    }
    async fn list_users(&self) -> Result<Vec<agentos_core::types::UserRecord>, StorageError> {
        Ok(Vec::new())
    }
    async fn update_last_login(&self, _user_id: &str) -> Result<(), StorageError> {
        Ok(())
    }
    async fn delete_user(&self, _user_id: &str) -> Result<bool, StorageError> {
        Ok(false)
    }
}

fn make_test_content_loader() -> ContentLoader {
    let store = Arc::new(MockStorageBackend) as Arc<dyn StorageBackend>;
    ContentLoader::new(store, "run_001".to_string(), "main".to_string(), 2)
}

#[tokio::test]
async fn test_content_loader_creation() {
    let loader = make_test_content_loader();
    assert_eq!(loader.requires_content, 2);
}

#[tokio::test]
async fn test_content_loader_load_blob() {
    let loader = make_test_content_loader();
    let blob = loader.load_blob("blob_001").await.unwrap();
    assert_eq!(blob, vec![1, 2, 3]);
}

#[test]
fn test_content_loader_debug_clone() {
    let loader = make_test_content_loader();
    let _debug_str = format!("{:?}", loader);
    let _cloned = loader.clone();
}

// ═══════════════════════════════════════════════════════════════════
// ADR ⑦: PluginContext 新增 content_loader 字段
// ═══════════════════════════════════════════════════════════════════

#[test]
fn test_plugin_context_with_content_loader() {
    let loader = make_test_content_loader();
    let ctx = PluginContext::new(
        json!({"key": "value"}),
        json!({}),
        TenantContext::new("tenant_001", "session_001"),
        Uuid::new_v4(),
        loader,
    );
    assert_eq!(ctx.task_id, "");
    assert_eq!(ctx.session_id, "");
}

// ═══════════════════════════════════════════════════════════════════
// ADR ④: SQLite 四表模型
// ═══════════════════════════════════════════════════════════════════

#[test]
fn test_run_record_serialization() {
    let record = RunRecord {
        run_id: "run_001".to_string(),
        config_hash: "abc123".to_string(),
        status: RunStatus::Running,
        tenant_id: "tenant_001".to_string(),
        created_at: "2026-07-14T00:00:00Z".to_string(),
        ended_at: None,
        current_branch: "main".to_string(),
        current_seq: 0,
        metadata: None,
    };
    let json_str = serde_json::to_string(&record).unwrap();
    assert!(json_str.contains("run_001"));
    assert!(json_str.contains("running"));
    let deserialized: RunRecord = serde_json::from_str(&json_str).unwrap();
    assert_eq!(deserialized.run_id, "run_001");
}

#[test]
fn test_run_status_variants() {
    let variants = [
        RunStatus::Running,
        RunStatus::Suspended,
        RunStatus::Completed,
        RunStatus::Failed,
    ];
    assert_eq!(variants.len(), 4);
    let json_str = serde_json::to_string(&RunStatus::Suspended).unwrap();
    assert_eq!(json_str, "\"suspended\"");
}

#[test]
fn test_message_record_serialization() {
    let record = MessageRecord {
        message_id: "msg_001".to_string(),
        run_id: "run_001".to_string(),
        branch_id: "main".to_string(),
        seq_in_branch: 1,
        role: "user".to_string(),
        blob_id: Some("blob_001".to_string()),
        content_preview: Some("Hello".to_string()),
        created_at: "2026-07-14T00:00:00Z".to_string(),
        pipeline_id: None,
        tool_calls_json: None,
        tool_call_id: None,
        reasoning_content: None,
        status: None,
        error: None,
        tool_result_json: None,
    };
    let json_str = serde_json::to_string(&record).unwrap();
    assert!(json_str.contains("msg_001"));
    assert!(json_str.contains("main"));
    assert!(json_str.contains("seq_in_branch"));
}

#[test]
fn test_trace_entry_serialization() {
    let entry = TraceEntry {
        trace_id: "trace_001".to_string(),
        run_id: "run_001".to_string(),
        branch_id: "main".to_string(),
        seq_in_branch: 1,
        plugin_id: "plugin_001".to_string(),
        patch_type: PatchType::StateUpdate,
        patch_data: json!({"key": "value"}),
        created_at: "2026-07-14T00:00:00Z".to_string(),
    };
    let json_str = serde_json::to_string(&entry).unwrap();
    assert!(json_str.contains("trace_001"));
    assert!(json_str.contains("state_update"));
    assert!(json_str.contains("plugin_001"));
}

#[test]
fn test_patch_type_variants() {
    let variants = [
        PatchType::StateUpdate,
        PatchType::RouteSignal,
        PatchType::Error,
        PatchType::Lifecycle,
        PatchType::Rollback,
    ];
    assert_eq!(variants.len(), 5);
    let json_str = serde_json::to_string(&PatchType::Rollback).unwrap();
    assert_eq!(json_str, "\"rollback\"");
}

#[test]
fn test_blob_record_serialization() {
    let record = BlobRecord {
        blob_id: "blob_001".to_string(),
        mime_type: "text/plain".to_string(),
        size_bytes: 100,
        created_at: "2026-07-14T00:00:00Z".to_string(),
    };
    let json_str = serde_json::to_string(&record).unwrap();
    assert!(json_str.contains("blob_001"));
    assert!(json_str.contains("text/plain"));
}

// ═══════════════════════════════════════════════════════════════════
// ADR ⑤: 多分支模型
// ═══════════════════════════════════════════════════════════════════

#[test]
fn test_branch_model_rollback() {
    let branch = Branch {
        branch_id: "main.rollback.001".to_string(),
        run_id: "run_001".to_string(),
        parent_branch: Some("main".to_string()),
        parent_seq: Some(1),
        created_at: "2026-07-14T00:00:00Z".to_string(),
    };
    let json_str = serde_json::to_string(&branch).unwrap();
    assert!(json_str.contains("main.rollback.001"));
    assert!(json_str.contains("parent_branch"));
    assert!(json_str.contains("parent_seq"));
    let deserialized: Branch = serde_json::from_str(&json_str).unwrap();
    assert_eq!(deserialized.parent_branch, Some("main".to_string()));
    assert_eq!(deserialized.parent_seq, Some(1));
}

#[test]
fn test_branch_model_root_branch() {
    let branch = Branch {
        branch_id: "main".to_string(),
        run_id: "run_001".to_string(),
        parent_branch: None,
        parent_seq: None,
        created_at: "2026-07-14T00:00:00Z".to_string(),
    };
    assert!(branch.parent_branch.is_none());
    assert!(branch.parent_seq.is_none());
}

// ═══════════════════════════════════════════════════════════════════
// ADR ⑧: 所有插件支持 InProcess + Sidecar 双路径
// ═══════════════════════════════════════════════════════════════════

#[test]
fn test_pipeline_plugin_in_process() {
    let manifest = make_test_manifest(HostType::InProcess, PluginType::Pipeline, None);
    assert_eq!(manifest.host_type, HostType::InProcess);
}

#[test]
fn test_pipeline_plugin_sidecar() {
    let manifest = make_test_manifest(HostType::Sidecar, PluginType::Pipeline, None);
    assert_eq!(manifest.host_type, HostType::Sidecar);
}

#[test]
fn test_tool_plugin_in_process() {
    let manifest = make_test_manifest(HostType::InProcess, PluginType::Tool, None);
    assert_eq!(manifest.host_type, HostType::InProcess);
}

#[test]
fn test_tool_plugin_sidecar() {
    let manifest = make_test_manifest(HostType::Sidecar, PluginType::Tool, None);
    assert_eq!(manifest.host_type, HostType::Sidecar);
}

#[test]
fn test_composite_plugin_in_process() {
    let manifest = make_test_manifest(HostType::InProcess, PluginType::Composite, None);
    assert_eq!(manifest.host_type, HostType::InProcess);
}

// ═══════════════════════════════════════════════════════════════════
// ADR ⑥: 组合插件 YAML 配置类型
// ═══════════════════════════════════════════════════════════════════

#[test]
fn test_composite_step_serialization() {
    let step = CompositeStep {
        name: "retrieve".to_string(),
        plugin: "knowledge_search".to_string(),
        inputs: json!({"query": "{{state.user_query}}"}),
        outputs: {
            let mut m = HashMap::new();
            m.insert("context".to_string(), "{{result.data}}".to_string());
            m
        },
    };
    let json_str = serde_json::to_string(&step).unwrap();
    assert!(json_str.contains("retrieve"));
    assert!(json_str.contains("knowledge_search"));
}

#[test]
fn test_composite_plugin_config_serialization() {
    let config = CompositePluginConfig {
        steps: vec![CompositeStep {
            name: "llm_call".to_string(),
            plugin: "llm_call".to_string(),
            inputs: json!({"messages": []}),
            outputs: HashMap::new(),
        }],
    };
    let json_str = serde_json::to_string(&config).unwrap();
    assert!(json_str.contains("llm_call"));
    assert!(json_str.contains("steps"));
}

// ═══════════════════════════════════════════════════════════════════
// ADR ⑧: HostType 序列化验证
// ═══════════════════════════════════════════════════════════════════

#[test]
fn test_host_type_serialization() {
    assert_eq!(
        serde_json::to_string(&HostType::InProcess).unwrap(),
        "\"in_process\""
    );
    assert_eq!(
        serde_json::to_string(&HostType::Sidecar).unwrap(),
        "\"sidecar\""
    );
}
