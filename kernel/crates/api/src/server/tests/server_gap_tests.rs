use super::*;
use agentos_core::traits::PluginManifest;

/// 只注入指定方法故障、其余全部转发真实 SqliteStore 的探针 store。
///
/// `fail_checkpoint`：load_latest_checkpoint 报错（冷恢复降级分支）。
/// `fail_traces`：get_step_traces_by_pipeline 报错（回放失败分支）。
/// `fail_state`：load_pipeline_state 报错（标量基线不完整分支）。
/// `fail_messages`：get_messages_by_pipeline 报错（对话历史不完整分支）。
struct ProbeStore {
    inner: Arc<agentos_engine::SqliteStore>,
    fail_checkpoint: std::sync::atomic::AtomicBool,
    fail_traces: std::sync::atomic::AtomicBool,
    fail_state: std::sync::atomic::AtomicBool,
    fail_messages: std::sync::atomic::AtomicBool,
}

impl ProbeStore {
    fn new() -> Self {
        Self {
            inner: Arc::new(agentos_engine::SqliteStore::open_memory().unwrap()),
            fail_checkpoint: std::sync::atomic::AtomicBool::new(false),
            fail_traces: std::sync::atomic::AtomicBool::new(false),
            fail_state: std::sync::atomic::AtomicBool::new(false),
            fail_messages: std::sync::atomic::AtomicBool::new(false),
        }
    }

    fn inject(&self, which: &str) {
        let flag = match which {
            "checkpoint" => &self.fail_checkpoint,
            "traces" => &self.fail_traces,
            "state" => &self.fail_state,
            "messages" => &self.fail_messages,
            other => panic!("unknown flag {other}"),
        };
        flag.store(true, std::sync::atomic::Ordering::SeqCst);
    }

    fn err(&self, what: &str) -> agentos_core::types::StorageError {
        agentos_core::types::StorageError::Database(format!("injected {what} failure"))
    }
}

#[async_trait::async_trait]
impl agentos_core::traits::StorageBackend for ProbeStore {
    async fn load_latest_checkpoint(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
    ) -> Result<Option<(i64, serde_json::Value)>, agentos_core::types::StorageError> {
        if self
            .fail_checkpoint
            .load(std::sync::atomic::Ordering::SeqCst)
        {
            return Err(self.err("checkpoint"));
        }
        agentos_core::traits::StorageBackend::load_latest_checkpoint(
            self.inner.as_ref(),
            pipeline_id,
            tenant_id,
        )
        .await
    }

    async fn get_step_traces_by_pipeline(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<agentos_core::types::TraceEntry>, agentos_core::types::StorageError> {
        if self.fail_traces.load(std::sync::atomic::Ordering::SeqCst) {
            return Err(self.err("traces"));
        }
        agentos_core::traits::StorageBackend::get_step_traces_by_pipeline(
            self.inner.as_ref(),
            pipeline_id,
            tenant_id,
        )
        .await
    }

    async fn load_pipeline_state(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
    ) -> Result<
        std::collections::HashMap<String, serde_json::Value>,
        agentos_core::types::StorageError,
    > {
        if self.fail_state.load(std::sync::atomic::Ordering::SeqCst) {
            return Err(self.err("state"));
        }
        agentos_core::traits::StorageBackend::load_pipeline_state(
            self.inner.as_ref(),
            pipeline_id,
            tenant_id,
        )
        .await
    }

    async fn get_messages_by_pipeline(
        &self,
        pipeline_id: &str,
        opts: MessageQueryOpts,
    ) -> Result<Vec<agentos_core::types::MessageRecord>, agentos_core::types::StorageError> {
        if self.fail_messages.load(std::sync::atomic::Ordering::SeqCst) {
            return Err(self.err("messages"));
        }
        agentos_core::traits::StorageBackend::get_messages_by_pipeline(
            self.inner.as_ref(),
            pipeline_id,
            opts,
        )
        .await
    }

    // ── trait 必需方法：转发真实实现（行为真实性，非全 mock） ──
    async fn get_run(
        &self,
        run_id: &str,
    ) -> Result<agentos_core::types::RunRecord, agentos_core::types::StorageError> {
        agentos_core::traits::StorageBackend::get_run(self.inner.as_ref(), run_id).await
    }
    async fn get_blob(&self, blob_id: &str) -> Result<Vec<u8>, agentos_core::types::StorageError> {
        agentos_core::traits::StorageBackend::get_blob(self.inner.as_ref(), blob_id).await
    }
    async fn append_trace(
        &self,
        entry: agentos_core::types::TraceEntry,
    ) -> Result<(), agentos_core::types::StorageError> {
        agentos_core::traits::StorageBackend::append_trace(self.inner.as_ref(), entry).await
    }
    async fn record_run_start(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
        run_id: &str,
        config_hash: &str,
        started_at: &str,
    ) -> Result<(), agentos_core::types::StorageError> {
        self.inner
            .record_run_start(pipeline_id, tenant_id, run_id, config_hash, started_at)
    }
    async fn store_blob(
        &self,
        data: &[u8],
        mime_type: &str,
    ) -> Result<String, agentos_core::types::StorageError> {
        agentos_core::traits::StorageBackend::store_blob(self.inner.as_ref(), data, mime_type).await
    }
    async fn create_session(
        &self,
        session: &agentos_core::types::SessionRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        agentos_core::traits::StorageBackend::create_session(self.inner.as_ref(), session).await
    }
    async fn get_session(
        &self,
        thread_id: &str,
    ) -> Result<Option<agentos_core::types::SessionRecord>, agentos_core::types::StorageError> {
        agentos_core::traits::StorageBackend::get_session(self.inner.as_ref(), thread_id).await
    }
    async fn list_sessions(
        &self,
        filter: agentos_core::traits::SessionListFilter,
    ) -> Result<Vec<agentos_core::types::SessionRecord>, agentos_core::types::StorageError> {
        agentos_core::traits::StorageBackend::list_sessions(self.inner.as_ref(), filter).await
    }
    async fn update_session(
        &self,
        session: &agentos_core::types::SessionRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        agentos_core::traits::StorageBackend::update_session(self.inner.as_ref(), session).await
    }
    async fn delete_session(
        &self,
        thread_id: &str,
    ) -> Result<Vec<String>, agentos_core::types::StorageError> {
        agentos_core::traits::StorageBackend::delete_session(self.inner.as_ref(), thread_id).await
    }
    async fn link_pipeline_session(
        &self,
        pipeline_id: &str,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<(), agentos_core::types::StorageError> {
        agentos_core::traits::StorageBackend::link_pipeline_session(
            self.inner.as_ref(),
            pipeline_id,
            thread_id,
            tenant_id,
        )
        .await
    }
    async fn list_pipeline_ids_by_thread(
        &self,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<String>, agentos_core::types::StorageError> {
        agentos_core::traits::StorageBackend::list_pipeline_ids_by_thread(
            self.inner.as_ref(),
            thread_id,
            tenant_id,
        )
        .await
    }
    async fn get_thread_id_by_pipeline(
        &self,
        pipeline_id: &str,
    ) -> Result<Option<String>, agentos_core::types::StorageError> {
        agentos_core::traits::StorageBackend::get_thread_id_by_pipeline(
            self.inner.as_ref(),
            pipeline_id,
        )
        .await
    }
    async fn get_step_traces_by_thread(
        &self,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<agentos_core::types::TraceEntry>, agentos_core::types::StorageError> {
        agentos_core::traits::StorageBackend::get_step_traces_by_thread(
            self.inner.as_ref(),
            thread_id,
            tenant_id,
        )
        .await
    }
    async fn create_user(
        &self,
        user: &agentos_core::types::UserRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        agentos_core::traits::StorageBackend::create_user(self.inner.as_ref(), user).await
    }
    async fn get_user_by_id(
        &self,
        user_id: &str,
    ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        agentos_core::traits::StorageBackend::get_user_by_id(self.inner.as_ref(), user_id).await
    }
    async fn get_user_by_username(
        &self,
        username: &str,
    ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        agentos_core::traits::StorageBackend::get_user_by_username(self.inner.as_ref(), username)
            .await
    }
    async fn list_users(
        &self,
    ) -> Result<Vec<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        agentos_core::traits::StorageBackend::list_users(self.inner.as_ref()).await
    }
    async fn update_user_password(
        &self,
        user_id: &str,
        password_hash: &str,
        must_change_password: bool,
    ) -> Result<bool, agentos_core::types::StorageError> {
        agentos_core::traits::StorageBackend::update_user_password(
            self.inner.as_ref(),
            user_id,
            password_hash,
            must_change_password,
        )
        .await
    }
    async fn update_last_login(
        &self,
        user_id: &str,
    ) -> Result<(), agentos_core::types::StorageError> {
        agentos_core::traits::StorageBackend::update_last_login(self.inner.as_ref(), user_id).await
    }
    async fn delete_user(&self, user_id: &str) -> Result<bool, agentos_core::types::StorageError> {
        agentos_core::traits::StorageBackend::delete_user(self.inner.as_ref(), user_id).await
    }
}

// ── compute_config_fingerprint（管道热重载指纹：配置 mtime ⊕ manifests 摘要）──

/// 指纹：steps 目录内的 yaml/yml 文件参与（非 yaml 不参与）；同状态稳定。
///
/// 须钉桩用户根：autonomous.yaml 生效落点「用户空间优先」，而钉桩 guard 持
/// 进程级环境变量+锁——不钉桩时并发线程上其他测试的钉桩/恢复会翻转本测试
/// 看到的用户根，两个时刻解析到不同文件即指纹漂移（并发假红）。
#[test]
fn config_fingerprint_tracks_steps_files_and_is_stable() {
    let tmp = tempfile::tempdir().unwrap();
    let _user_root = crate::test_env::pin_user_root(tmp.path());
    let config = tmp.path().join("config");
    std::fs::create_dir_all(config.join("pipelines")).unwrap();
    std::fs::create_dir_all(config.join("steps")).unwrap();
    std::fs::write(config.join("pipelines/autonomous.yaml"), "name: a\n").unwrap();
    // 非 yaml 文件不参与指纹
    std::fs::write(config.join("steps/README.md"), "# doc\n").unwrap();

    let before = compute_config_fingerprint(&config, &[]);
    assert_eq!(
        before,
        compute_config_fingerprint(&config, &[]),
        "同状态指纹必须稳定"
    );

    // 加一个 yaml step 文件 → 指纹必须变
    std::fs::write(config.join("steps/common.yaml"), "id: c\n").unwrap();
    let after = compute_config_fingerprint(&config, &[]);
    assert_ne!(before, after, "steps 目录新增 yaml 必须改变指纹");
    assert_eq!(
        after,
        compute_config_fingerprint(&config, &[]),
        "新状态指纹同样稳定"
    );

    // 再加 .yml（另一种扩展名同样参与）
    std::fs::write(config.join("steps/extra.yml"), "id: e\n").unwrap();
    assert_ne!(
        after,
        compute_config_fingerprint(&config, &[]),
        ".yml 也必须参与指纹"
    );
}

/// 目录不存在（无 steps/）不 panic，指纹仍可算。
#[test]
fn config_fingerprint_without_steps_dir_does_not_panic() {
    let tmp = tempfile::tempdir().unwrap();
    let config = tmp.path().join("config");
    std::fs::create_dir_all(config.join("pipelines")).unwrap();
    let _ = compute_config_fingerprint(&config, &[]);
    let _ = compute_config_fingerprint(&tmp.path().join("nonexistent"), &[]);
}

/// B6 指纹 manifests 维：声明面摘要内容敏感（能力变更必失效）、集合序不敏感
/// （watcher 重排合并不虚警）、内容不变则指纹不变（纯代码变更命中缓存）。
#[test]
fn config_fingerprint_tracks_manifests_summary() {
    let tmp = tempfile::tempdir().unwrap();
    let _user_root = crate::test_env::pin_user_root(tmp.path());
    let config = tmp.path().join("config");
    std::fs::create_dir_all(config.join("pipelines")).unwrap();
    std::fs::write(config.join("pipelines/autonomous.yaml"), "name: a\n").unwrap();

    let mk = |v: serde_json::Value| -> agentos_core::traits::PluginManifest {
        serde_json::from_value(v).expect("valid manifest")
    };
    let plain_a = mk(serde_json::json!({
        "id": "alpha", "name": "alpha", "version": "1.0.0",
        "plugin_type": "pipeline", "language": "python",
        "host_type": "sidecar", "entry": "x", "capabilities": {},
    }));
    let plain_b = mk(serde_json::json!({
        "id": "beta", "name": "beta", "version": "1.0.0",
        "plugin_type": "pipeline", "language": "python",
        "host_type": "sidecar", "entry": "x", "capabilities": {},
    }));
    let step_b = mk(serde_json::json!({
        "id": "beta", "name": "beta", "version": "1.0.0",
        "plugin_type": "pipeline", "language": "python",
        "host_type": "sidecar", "entry": "x",
        "capabilities": {"steps": [{"name": "beta.fold"}]},
    }));

    let base = compute_config_fingerprint(&config, &[plain_a.clone(), plain_b.clone()]);
    assert_eq!(
        base,
        compute_config_fingerprint(&config, &[plain_b.clone(), plain_a.clone()]),
        "manifests 集合序变化不得虚警（摘要按 id 排序）"
    );
    assert_ne!(
        base,
        compute_config_fingerprint(&config, &[plain_a.clone(), step_b.clone()]),
        "同 id 插件声明面变更（能力声明）必须改变指纹"
    );
    assert_ne!(
        base,
        compute_config_fingerprint(&config, &[plain_a]),
        "新插件热注册（集合增员）必须改变指纹"
    );
}

// ── 冷恢复四条降级/失败分支 ──

fn probe_state(store: Arc<ProbeStore>) -> AppState {
    let mut state = AppState::new();
    state.store = Some(store);
    state
}

/// checkpoint 读失败：不当作"无 checkpoint"静默降级——继续走 traces 回放
///（error 留痕），trace 里的标量基线仍被恢复。
#[tokio::test]
async fn cold_recovery_checkpoint_failure_degrades_to_traces() {
    let probe = Arc::new(ProbeStore::new());
    // DR 重建入口条件 = state 与消息双空：本测试不落 state 行与消息槽行，
    // traces 按 pipeline_id 直查回放（traces 已是 pipeline 级 op 流）。
    agentos_core::traits::StorageBackend::append_trace(
        probe.inner.as_ref(),
        agentos_core::types::TraceEntry {
            trace_id: "t1".to_string(),
            pipeline_id: "pipe_cp_fail".to_string(),
            // 写入侧 seq 恒 0，存储层按 (pipeline_id, MAX(seq)+1) 分配日志序
            seq: 0,
            plugin_id: "seed".to_string(),
            patch_type: agentos_core::types::PatchType::StateUpdate,
            patch_data: serde_json::json!({"from_trace": true}),
            created_at: "2026-09-15T00:00:00Z".to_string(),
        },
    )
    .await
    .unwrap();
    probe.inject("checkpoint");

    let _state = probe_state(probe.clone());
    let store: Arc<dyn agentos_core::traits::StorageBackend> = probe.clone();
    let recovered = stage_recover_history(
        serde_json::json!({}),
        &store,
        "msg",
        "pipe_cp_fail",
        "default",
        "",
        false,
        None,
        &std::collections::HashSet::new(),
        "run-cr",
    )
    .await
    .expect("恢复失败不得上抛（降级继续）");
    assert_eq!(
        recovered["from_trace"], true,
        "checkpoint 读失败后必须继续 traces 回放，不得静默丢标量基线: {recovered}"
    );
}

/// traces 回放失败：显式 error 暴露（bug 信号），函数仍返回可用初始 state。
#[tokio::test]
async fn cold_recovery_traces_failure_keeps_going() {
    let probe = Arc::new(ProbeStore::new());
    probe.inject("checkpoint");
    probe.inject("traces");

    let _state = probe_state(probe.clone());
    let store: Arc<dyn agentos_core::traits::StorageBackend> = probe.clone();
    let recovered = stage_recover_history(
        serde_json::json!({}),
        &store,
        "msg",
        "pipe_tr_fail",
        "default",
        "",
        false,
        None,
        &std::collections::HashSet::new(),
        "run-cr",
    )
    .await
    .expect("恢复失败不得上抛（降级继续）");
    assert!(
        recovered.is_object(),
        "traces 回放失败不得 panic，仍返回初始 state: {recovered}"
    );
}

/// pipeline_state 读失败：标量基线不完整但继续（不阻断本轮执行）。
#[tokio::test]
async fn cold_recovery_state_table_failure_keeps_going() {
    let probe = Arc::new(ProbeStore::new());
    probe.inject("checkpoint");
    probe.inject("state");

    let _state = probe_state(probe.clone());
    let store: Arc<dyn agentos_core::traits::StorageBackend> = probe.clone();
    let recovered = stage_recover_history(
        serde_json::json!({}),
        &store,
        "msg",
        "pipe_st_fail",
        "default",
        "",
        false,
        None,
        &std::collections::HashSet::new(),
        "run-cr",
    )
    .await
    .expect("恢复失败不得上抛（降级继续）");
    assert!(recovered.is_object(), "state 表读失败不得 panic");
}

/// messages 读失败：对话历史不完整但继续。
#[tokio::test]
async fn cold_recovery_message_history_failure_keeps_going() {
    let probe = Arc::new(ProbeStore::new());
    probe.inject("checkpoint");
    probe.inject("messages");

    let _state = probe_state(probe.clone());
    let store: Arc<dyn agentos_core::traits::StorageBackend> = probe.clone();
    let recovered = stage_recover_history(
        serde_json::json!({}),
        &store,
        "msg",
        "pipe_msg_fail",
        "default",
        "",
        false,
        None,
        &std::collections::HashSet::new(),
        "run-cr",
    )
    .await
    .expect("恢复失败不得上抛（降级继续）");
    assert!(recovered.is_object(), "messages 读失败不得 panic");
}

// ── 会话级 / 任务级 execution_context 注入（stage_build_initial_state） ──

fn ec_manifest(id: &str, meta_key: &str, exec_path: &str) -> PluginManifest {
    serde_json::from_value(serde_json::json!({
        "id": id, "name": id, "version": "1.0.0",
        "plugin_type": "system", "language": "python",
        "host_type": "sidecar", "entry": "x",
        "capabilities": {},
        "contributes": {"thread_fields": [
            {"name": "workspace_mode", "x_metadata_key": meta_key,
             "x_execution_path": exec_path}
        ]},
    }))
    .expect("valid manifest")
}

async fn seed_ec_session(
    sqlite: &Arc<agentos_engine::SqliteStore>,
    thread_id: &str,
    pipeline_id: &str,
    metadata: Option<serde_json::Value>,
) {
    sqlite
        .create_session(&agentos_core::types::SessionRecord {
            thread_id: thread_id.to_string(),
            title: None,
            intent: None,
            current_state: "active".to_string(),
            agent_id: None,
            active_pipeline_id: Some(pipeline_id.to_string()),
            pipeline_ids: vec![pipeline_id.to_string()],
            metadata,
            created_at: "2026-09-15T00:00:00Z".to_string(),
            updated_at: "2026-09-15T00:00:00Z".to_string(),
            last_active_at: None,
        })
        .await
        .unwrap();
}

/// 会话级注入：enabled 插件的 thread_fields 声明把 thread metadata 值按
/// x_execution_path 写入 execution_context；disabled 插件的声明不生效
/// （声明驱动 + 启停门）。
#[tokio::test]
async fn session_level_execution_context_injected_by_declaration() {
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn agentos_core::traits::StorageBackend> = sqlite.clone();
    seed_ec_session(
        &sqlite,
        "thread-ec",
        "pipe_ec",
        Some(serde_json::json!({"ws_mode": "isolated"})),
    )
    .await;

    let mut state = AppState::new();
    state.store = Some(store.clone());
    // 两个插件：enabled 的声明生效；disabled 的声明必须被启停门挡住
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![
        ec_manifest("ws_on", "ws_mode", "workspace.mode"),
        ec_manifest("ws_off", "other_mode", "disabled.path"),
    ]));
    state
        .enabled_plugin_ids
        .write()
        .await
        .insert("ws_on".to_string());

    let recovered = stage_build_initial_state(
        &state,
        &store,
        "hello",
        "pipe_ec",
        "thread-ec",
        "msg-1",
        "u1",
        "",
        None,
        "run-1",
    )
    .await;
    assert_eq!(
        recovered["execution_context"]["workspace"]["mode"], "isolated",
        "enabled 插件声明必须注入（按 x_execution_path 落点）: {recovered}"
    );
    assert!(
        recovered["execution_context"].get("disabled").is_none(),
        "disabled 插件的声明不得生效: {recovered}"
    );
    // 基线键齐备（行为契约）
    for key in [
        "message",
        "input",
        "run_status",
        "run_id",
        "pipeline_id",
        "session_id",
        "user_id",
        "message_id",
    ] {
        assert!(recovered.get(key).is_some(), "基线键 {key} 必须存在");
    }
    assert_eq!(recovered["run_status"], "running");
    assert_eq!(recovered["ended"], false);
    assert_eq!(recovered["pipeline_id"], "pipe_ec");
    assert_eq!(recovered["session_id"], "thread-ec");
}

/// 消息级 execution_context 逐键并入会话级种子（消息级显式键 > 会话级
/// 同键，未携带键保留会话级）；非对象/空对象不注入。
#[tokio::test]
async fn message_level_execution_context_merges_per_key_over_session_level() {
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn agentos_core::traits::StorageBackend> = sqlite.clone();
    seed_ec_session(
        &sqlite,
        "thread-ec2",
        "pipe_ec2",
        Some(serde_json::json!({"ws_mode": "session_value"})),
    )
    .await;
    let mut state = AppState::new();
    state.store = Some(store.clone());
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![ec_manifest(
        "ws_on",
        "ws_mode",
        "workspace.mode",
    )]));
    state
        .enabled_plugin_ids
        .write()
        .await
        .insert("ws_on".to_string());

    let task_ec = serde_json::json!({"task": {"id": "t-1"}});
    let recovered = stage_build_initial_state(
        &state,
        &store,
        "hi",
        "pipe_ec2",
        "thread-ec2",
        "msg-2",
        "u1",
        "",
        Some(&task_ec),
        "run-2",
    )
    .await;
    assert_eq!(
        recovered["execution_context"]["task"]["id"], "t-1",
        "消息级显式键落位: {recovered}"
    );
    assert_eq!(
        recovered["execution_context"]["workspace"]["mode"], "session_value",
        "消息级未携带的键保留会话级种子（逐键合并，非整键替换）: {recovered}"
    );

    // 非对象（字符串）不注入 → 保留会话级值
    let bad = serde_json::json!("not-an-object");
    let recovered = stage_build_initial_state(
        &state,
        &store,
        "hi",
        "pipe_ec2",
        "thread-ec2",
        "msg-3",
        "u1",
        "",
        Some(&bad),
        "run-3",
    )
    .await;
    assert_eq!(
        recovered["execution_context"]["workspace"]["mode"], "session_value",
        "非对象任务级上下文不得注入: {recovered}"
    );

    // 空对象同样不注入
    let empty = serde_json::json!({});
    let recovered = stage_build_initial_state(
        &state,
        &store,
        "hi",
        "pipe_ec2",
        "thread-ec2",
        "msg-4",
        "u1",
        "",
        Some(&empty),
        "run-4",
    )
    .await;
    assert_eq!(
        recovered["execution_context"]["workspace"]["mode"], "session_value",
        "空对象任务级上下文不得注入: {recovered}"
    );
}

/// 会话不存在 / 会话存在但无 metadata：不注入 execution_context（不 panic）。
#[tokio::test]
async fn missing_session_or_metadata_skips_context_injection() {
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn agentos_core::traits::StorageBackend> = sqlite.clone();
    let mut state = AppState::new();
    state.store = Some(store.clone());
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![ec_manifest(
        "ws_on",
        "ws_mode",
        "workspace.mode",
    )]));
    state
        .enabled_plugin_ids
        .write()
        .await
        .insert("ws_on".to_string());

    // 会话不存在
    let recovered = stage_build_initial_state(
        &state,
        &store,
        "hi",
        "pipe_none",
        "thread-missing",
        "m",
        "u",
        "",
        None,
        "run",
    )
    .await;
    assert!(
        recovered.get("execution_context").is_none(),
        "无会话不得注入 execution_context: {recovered}"
    );

    // 会话存在但 metadata = None
    seed_ec_session(&sqlite, "thread-nometa", "pipe_nm", None).await;
    let recovered = stage_build_initial_state(
        &state,
        &store,
        "hi",
        "pipe_nm",
        "thread-nometa",
        "m",
        "u",
        "",
        None,
        "run",
    )
    .await;
    assert!(
        recovered.get("execution_context").is_none(),
        "metadata 缺席不得注入: {recovered}"
    );
}

// ── merge_patch 剩余分支（类型替换 / 非对象 patch） ──

/// 目标非对象时整体替换为对象再合并；键已存在且一侧非对象 → 值替换；
/// patch 非对象 → 整体替换。
#[test]
fn merge_patch_type_replacement_and_non_object_patch() {
    // 目标非对象 → 先重置成对象再合并
    let mut target = serde_json::json!("scalar");
    merge_patch(&mut target, &serde_json::json!({"a": 1}));
    assert_eq!(target, serde_json::json!({"a": 1}));

    // 已存在键两侧均为对象 → 递归合并；一侧非对象 → 替换
    let mut target = serde_json::json!({"obj": {"a": 1}, "leaf": "old"});
    merge_patch(
        &mut target,
        &serde_json::json!({"obj": {"b": 2}, "leaf": {"nested": true}}),
    );
    assert_eq!(target["obj"]["a"], 1, "对象递归合并不丢旧键");
    assert_eq!(target["obj"]["b"], 2);
    assert_eq!(
        target["leaf"],
        serde_json::json!({"nested": true}),
        "一侧非对象必须整体替换"
    );

    // patch 非对象 → 整体替换（含数组）
    let mut target = serde_json::json!({"a": 1});
    merge_patch(&mut target, &serde_json::json!([1, 2]));
    assert_eq!(target, serde_json::json!([1, 2]));

    // messages 键跳过（队列真值在表）
    let mut target = serde_json::json!({"messages": [1]});
    merge_patch(&mut target, &serde_json::json!({"messages": [], "x": 1}));
    assert_eq!(
        target["messages"],
        serde_json::json!([1]),
        "messages 不参与标量回放"
    );
    assert_eq!(target["x"], 1);
}

// ── 落库重试：失败逐次还原内存（不留半条消息） ──

/// messages 表缺失 → user 消息落库重试耗尽上抛"未受理"语义；
/// 内存 messages 必须被还原（不留半条消息）。
#[tokio::test]
async fn user_append_failure_restores_memory_snapshot() {
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    sqlite
        .with_conn::<(), String>(|conn| {
            conn.execute("DROP TABLE message_slots", [])
                .map(|_| ())
                .map_err(|e| e.to_string())
        })
        .unwrap();
    let store: Arc<dyn agentos_core::traits::StorageBackend> = sqlite;

    let result = stage_recover_history(
        serde_json::json!({"pipeline_id": "pipe_append_fail", "messages": [{"role": "assistant"}]}),
        &store,
        "落库失败的新消息",
        "pipe_append_fail",
        "default",
        "",
        false,
        None,
        &std::collections::HashSet::new(),
        "run-append",
    )
    .await;
    let outcome = result.expect_err("落库失败重试耗尽必须上抛（消息未受理）");
    assert!(outcome.failed, "必须是失败 outcome: {outcome:?}");
    assert!(
        outcome.content.contains("未受理"),
        "错误文案必须表达「消息未受理」: {}",
        outcome.content
    );
}

/// 无 messages 快照时失败 → 还原为"无 messages 键"（不残留空数组）。
/// 与上一用例形成两组区分度输入（有快照 / 无快照）。
#[tokio::test]
async fn user_append_failure_without_snapshot_leaves_no_half_message() {
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    sqlite
        .with_conn::<(), String>(|conn| {
            conn.execute("DROP TABLE message_slots", [])
                .map(|_| ())
                .map_err(|e| e.to_string())
        })
        .unwrap();
    let store: Arc<dyn agentos_core::traits::StorageBackend> = sqlite;

    let result = stage_recover_history(
        serde_json::json!({"pipeline_id": "pipe_nosnap", "other": 1}),
        &store,
        "m2",
        "pipe_nosnap",
        "default",
        "",
        false,
        None,
        &std::collections::HashSet::new(),
        "run-nosnap",
    )
    .await;
    assert!(
        result.is_err(),
        "无快照时落库失败同样必须上抛（不静默成功）"
    );
}
