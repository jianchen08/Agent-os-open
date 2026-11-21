// @feature: FP-0.2.内核API面 | @vision: V3 可嵌入 | @ci: rust-test
use super::*;

/// 带 contributes.thread_fields 声明的测试 manifest（模拟 workspace_lifecycle /
/// isolation 插件的声明面）。
fn thread_field_manifest(
    id: &str,
    fields: serde_json::Value,
) -> agentos_core::traits::PluginManifest {
    agentos_core::traits::PluginManifest {
        force_include_tools: Vec::new(),
        state: None,
        id: id.to_string(),
        name: id.to_string(),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: agentos_core::traits::PluginType::Pipeline,
        language: "python".to_string(),
        host_type: agentos_core::traits::HostType::Sidecar,
        host_group: None,
        entry: "python server.py".to_string(),
        capabilities: Default::default(),
        requires_services: vec![],
        permissions: Default::default(),
        priority: 100,
        mcp: None,
        lifecycle: None,
        native: None,
        restricted_capabilities: vec![],
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: Some(fields),
        enabled: None,
        activation: None,
        persistent_fields: vec![],
        export_fields: vec![],
        aux_venvs: Vec::new(),
        provides: None,
        visibility: Default::default(),
    }
}

#[tokio::test]
async fn session_execution_context_assembled_from_thread_field_declarations() {
    // 声明驱动组装：metadata 值按 x_metadata_key → x_execution_path 声明路径
    // 写入 execution_context——内核不认识 workspace/isolation 具体键。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let now = "2026-08-28T00:00:00Z";
    store
        .create_session(&agentos_core::types::SessionRecord {
            thread_id: "thread-ec1".to_string(),
            title: None,
            intent: None,
            current_state: "active".to_string(),
            agent_id: None,
            active_pipeline_id: None,
            pipeline_ids: vec![],
            metadata: Some(json!({
                "workspace": "D:/proj/demo",
                "workspace_mode": "plain",
                "isolation_mode": "isolated",
            })),
            created_at: now.to_string(),
            updated_at: now.to_string(),
            last_active_at: Some(now.to_string()),
        })
        .await
        .unwrap();
    let state = AppState::new();
    state.manifests.write().await.push(thread_field_manifest(
        "ws_lifecycle",
        json!({"thread_fields": [
            {"name": "workspace", "x_metadata_key": "workspace", "x_execution_path": "workspace.source_path"},
            {"name": "workspaceMode", "x_metadata_key": "workspace_mode", "x_execution_path": "workspace.mode"},
        ]}),
    ));
    state.manifests.write().await.push(thread_field_manifest(
        "isolation",
        json!({"thread_fields": [
            {"name": "isolationMode", "x_metadata_key": "isolation_mode", "x_execution_path": "isolation.level"},
        ]}),
    ));
    state
        .enabled_plugin_ids
        .write()
        .await
        .extend(["ws_lifecycle".to_string(), "isolation".to_string()]);
    let st = stage_build_initial_state(
        &state,
        &store,
        "msg",
        "pipe_ec1",
        "thread-ec1",
        "m1",
        "u1",
        None,
        "run-ec1",
    )
    .await;
    assert_eq!(
        st["execution_context"]["workspace"]["source_path"],
        "D:/proj/demo"
    );
    assert_eq!(st["execution_context"]["workspace"]["mode"], "plain");
    assert_eq!(st["execution_context"]["isolation"]["level"], "isolated");
}

#[tokio::test]
async fn session_execution_context_no_kernel_mode_default() {
    // 行为变更（ADR 2026-08-28 有意修正）：metadata 未选模式时内核不再缺省
    // 塞 worktree——mode 键缺位，默认值归插件执行期（workspace_lifecycle → plain，
    // 与前端 x_guard on_empty=plain 对齐）。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let now = "2026-08-28T00:00:00Z";
    store
        .create_session(&agentos_core::types::SessionRecord {
            thread_id: "thread-ec2".to_string(),
            title: None,
            intent: None,
            current_state: "active".to_string(),
            agent_id: None,
            active_pipeline_id: None,
            pipeline_ids: vec![],
            metadata: Some(json!({ "workspace": "D:/proj/demo" })),
            created_at: now.to_string(),
            updated_at: now.to_string(),
            last_active_at: Some(now.to_string()),
        })
        .await
        .unwrap();
    let state = AppState::new();
    state.manifests.write().await.push(thread_field_manifest(
        "ws_lifecycle",
        json!({"thread_fields": [
            {"name": "workspace", "x_metadata_key": "workspace", "x_execution_path": "workspace.source_path"},
            {"name": "workspaceMode", "x_metadata_key": "workspace_mode", "x_execution_path": "workspace.mode"},
        ]}),
    ));
    state
        .enabled_plugin_ids
        .write()
        .await
        .insert("ws_lifecycle".to_string());
    let st = stage_build_initial_state(
        &state,
        &store,
        "msg",
        "pipe_ec2",
        "thread-ec2",
        "m1",
        "u1",
        None,
        "run-ec2",
    )
    .await;
    assert_eq!(
        st["execution_context"]["workspace"]["source_path"],
        "D:/proj/demo"
    );
    assert!(
        st["execution_context"]["workspace"].get("mode").is_none(),
        "内核不得代插件塞 mode 默认值"
    );
}

#[tokio::test]
async fn session_execution_context_disabled_plugin_declarations_ignored() {
    // 未启用插件的声明不参与组装（与 sessions schema 聚合的 enabled 过滤对齐）
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let now = "2026-08-28T00:00:00Z";
    store
        .create_session(&agentos_core::types::SessionRecord {
            thread_id: "thread-ec3".to_string(),
            title: None,
            intent: None,
            current_state: "active".to_string(),
            agent_id: None,
            active_pipeline_id: None,
            pipeline_ids: vec![],
            metadata: Some(json!({ "isolation_mode": "isolated" })),
            created_at: now.to_string(),
            updated_at: now.to_string(),
            last_active_at: Some(now.to_string()),
        })
        .await
        .unwrap();
    let state = AppState::new();
    state.manifests.write().await.push(thread_field_manifest(
        "isolation",
        json!({"thread_fields": [
            {"name": "isolationMode", "x_metadata_key": "isolation_mode", "x_execution_path": "isolation.level"},
        ]}),
    ));
    // isolation 未加入 enabled_plugin_ids
    let st = stage_build_initial_state(
        &state,
        &store,
        "msg",
        "pipe_ec3",
        "thread-ec3",
        "m1",
        "u1",
        None,
        "run-ec3",
    )
    .await;
    assert!(
        st.get("execution_context").is_none(),
        "未启用插件的声明不得组装 execution_context"
    );
}

#[tokio::test]
async fn message_execution_context_merges_per_key_over_session_level() {
    // 1a2 逐键合并：消息级显式键 > 会话级同键；消息级未携带的键保留会话级
    // 种子（会话级默认兜底，不得被整体替换丢弃）。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let now = "2026-08-28T00:00:00Z";
    store
        .create_session(&agentos_core::types::SessionRecord {
            thread_id: "thread-ec4".to_string(),
            title: None,
            intent: None,
            current_state: "active".to_string(),
            agent_id: None,
            active_pipeline_id: None,
            pipeline_ids: vec![],
            metadata: Some(json!({
                "workspace": "D:/proj/demo",
                "isolation_mode": "isolated",
            })),
            created_at: now.to_string(),
            updated_at: now.to_string(),
            last_active_at: Some(now.to_string()),
        })
        .await
        .unwrap();
    let state = AppState::new();
    state.manifests.write().await.push(thread_field_manifest(
        "ws_lifecycle",
        json!({"thread_fields": [
            {"name": "workspace", "x_metadata_key": "workspace", "x_execution_path": "workspace.source_path"},
        ]}),
    ));
    state.manifests.write().await.push(thread_field_manifest(
        "isolation",
        json!({"thread_fields": [
            {"name": "isolationMode", "x_metadata_key": "isolation_mode", "x_execution_path": "isolation.level"},
        ]}),
    ));
    state
        .enabled_plugin_ids
        .write()
        .await
        .extend(["ws_lifecycle".to_string(), "isolation".to_string()]);
    let message_ec = json!({
        "mode": "coding",
        "isolation": {"level": "non_isolated"},
    });
    let st = stage_build_initial_state(
        &state,
        &store,
        "msg",
        "pipe_ec4",
        "thread-ec4",
        "m1",
        "u1",
        Some(&message_ec),
        "run-ec4",
    )
    .await;
    assert_eq!(
        st["execution_context"]["mode"], "coding",
        "消息级显式键落位"
    );
    assert_eq!(
        st["execution_context"]["isolation"]["level"], "non_isolated",
        "消息级同键覆盖会话级（逐键，非整键替换）"
    );
    assert_eq!(
        st["execution_context"]["workspace"]["source_path"], "D:/proj/demo",
        "消息级未携带的键保留会话级种子"
    );
}

#[tokio::test]
async fn message_execution_context_lands_when_no_session_level_values() {
    // 会话无声明值（1a 不注入）时消息级 execution_context 原样落位（1a2）。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let now = "2026-08-28T00:00:00Z";
    store
        .create_session(&agentos_core::types::SessionRecord {
            thread_id: "thread-ec5".to_string(),
            title: None,
            intent: None,
            current_state: "active".to_string(),
            agent_id: None,
            active_pipeline_id: None,
            pipeline_ids: vec![],
            metadata: Some(json!({})),
            created_at: now.to_string(),
            updated_at: now.to_string(),
            last_active_at: Some(now.to_string()),
        })
        .await
        .unwrap();
    let state = AppState::new();
    let message_ec = json!({"mode": "coding"});
    let st = stage_build_initial_state(
        &state,
        &store,
        "msg",
        "pipe_ec5",
        "thread-ec5",
        "m1",
        "u1",
        Some(&message_ec),
        "run-ec5",
    )
    .await;
    assert_eq!(
        st["execution_context"],
        json!({"mode": "coding"}),
        "无会话级种子时消息级原样落位"
    );
}
