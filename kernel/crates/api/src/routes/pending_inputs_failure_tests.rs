//! 扫描 2026-08-27 辖区一 Should#18：pending-inputs 删除/清空前
//! `list_pending_inputs(...).unwrap_or_default()` 吞 DB 错 → cmid 丢 →
//! 排队中的 REST chat waiter 永不通知（挂死至超时）。
//! 修复契约：列表读取失败 = 无法安全逐出 → 跳过删除/清空并返回 503，
//! 排队条目保留（消费循环稍后仍会正常 pop 执行并回传 outcome），不再丢 cmid。

use super::*;
use agentos_core::traits::StorageBackend;
use agentos_core::types::{PendingInputRecord, PendingInputSource};
use axum::http::HeaderMap;

/// 委托真实 SqliteStore 的探针 store：仅 list_pending_inputs 可注入故障，
/// 其余方法全部转发真实实现（删除/清空走真库，非全 mock）。
struct PendingProbeStore {
    inner: std::sync::Arc<agentos_engine::SqliteStore>,
    fail_list: std::sync::atomic::AtomicBool,
}

impl PendingProbeStore {
    fn new() -> Self {
        Self {
            inner: std::sync::Arc::new(agentos_engine::SqliteStore::open_memory().unwrap()),
            fail_list: std::sync::atomic::AtomicBool::new(false),
        }
    }

    async fn seed(&self, pipeline_id: &str, id: &str, cmid: &str) {
        StorageBackend::enqueue_pending_input(
            self.inner.as_ref(),
            "default",
            pipeline_id,
            &PendingInputRecord {
                id: id.to_string(),
                pipeline_id: pipeline_id.to_string(),
                tenant_id: "default".to_string(),
                user_id: "u1".to_string(),
                content: "排队输入".to_string(),
                thread: "thread-probe".to_string(),
                source: PendingInputSource::Trigger,
                agent_id: "agentos".to_string(),
                route_id: pipeline_id.to_string(),
                thinking_strength: String::new(),
                client_message_id: cmid.to_string(),
                execution_context: None,
                state_overlay: None,
                pipeline_config_id: None,
                created_at: chrono::Utc::now().to_rfc3339(),
            },
        )
        .await
        .unwrap();
    }

    async fn listed(&self, pipeline_id: &str) -> Vec<PendingInputRecord> {
        StorageBackend::list_pending_inputs(self.inner.as_ref(), "default", pipeline_id)
            .await
            .unwrap()
    }
}

#[async_trait::async_trait]
impl StorageBackend for PendingProbeStore {
    async fn list_pending_inputs(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
    ) -> Result<Vec<PendingInputRecord>, agentos_core::types::StorageError> {
        if self.fail_list.load(std::sync::atomic::Ordering::SeqCst) {
            return Err(agentos_core::types::StorageError::Database(
                "injected list failure".to_string(),
            ));
        }
        StorageBackend::list_pending_inputs(self.inner.as_ref(), tenant_id, pipeline_id).await
    }
    async fn delete_pending_input(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        input_id: &str,
    ) -> Result<bool, agentos_core::types::StorageError> {
        StorageBackend::delete_pending_input(self.inner.as_ref(), tenant_id, pipeline_id, input_id)
            .await
    }
    async fn clear_pending_inputs(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
    ) -> Result<usize, agentos_core::types::StorageError> {
        StorageBackend::clear_pending_inputs(self.inner.as_ref(), tenant_id, pipeline_id).await
    }
    async fn get_thread_id_by_pipeline(
        &self,
        pipeline_id: &str,
    ) -> Result<Option<String>, agentos_core::types::StorageError> {
        StorageBackend::get_thread_id_by_pipeline(self.inner.as_ref(), pipeline_id).await
    }

    // ── trait 必需（无默认）方法：转发真实 store，保持行为真实性 ──
    async fn get_run(
        &self,
        run_id: &str,
    ) -> Result<agentos_core::types::RunRecord, agentos_core::types::StorageError> {
        self.inner.get_run(run_id).await
    }
    async fn get_messages_by_pipeline(
        &self,
        pipeline_id: &str,
        opts: agentos_core::traits::MessageQueryOpts,
    ) -> Result<Vec<agentos_core::types::MessageRecord>, agentos_core::types::StorageError> {
        self.inner.get_messages_by_pipeline(pipeline_id, opts).await
    }
    async fn get_blob(&self, blob_id: &str) -> Result<Vec<u8>, agentos_core::types::StorageError> {
        self.inner.get_blob(blob_id).await
    }
    async fn append_trace(
        &self,
        entry: agentos_core::types::TraceEntry,
    ) -> Result<(), agentos_core::types::StorageError> {
        self.inner.append_trace(entry).await
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
        StorageBackend::store_blob(self.inner.as_ref(), data, mime_type).await
    }
    async fn create_session(
        &self,
        session: &agentos_core::types::SessionRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        self.inner.create_session(session).await
    }
    async fn get_session(
        &self,
        thread_id: &str,
    ) -> Result<Option<agentos_core::types::SessionRecord>, agentos_core::types::StorageError> {
        self.inner.get_session(thread_id).await
    }
    async fn list_sessions(
        &self,
        filter: agentos_core::traits::SessionListFilter,
    ) -> Result<Vec<agentos_core::types::SessionRecord>, agentos_core::types::StorageError> {
        self.inner.list_sessions(filter).await
    }
    async fn update_session(
        &self,
        session: &agentos_core::types::SessionRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        self.inner.update_session(session).await
    }
    async fn delete_session(
        &self,
        thread_id: &str,
    ) -> Result<Vec<String>, agentos_core::types::StorageError> {
        self.inner.delete_session(thread_id).await
    }
    async fn link_pipeline_session(
        &self,
        pipeline_id: &str,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<(), agentos_core::types::StorageError> {
        self.inner
            .link_pipeline_session(pipeline_id, thread_id, tenant_id)
            .await
    }
    async fn list_pipeline_ids_by_thread(
        &self,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<String>, agentos_core::types::StorageError> {
        self.inner
            .list_pipeline_ids_by_thread(thread_id, tenant_id)
            .await
    }
    async fn get_step_traces_by_thread(
        &self,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<agentos_core::types::TraceEntry>, agentos_core::types::StorageError> {
        self.inner
            .get_step_traces_by_thread(thread_id, tenant_id)
            .await
    }
    async fn create_user(
        &self,
        user: &agentos_core::types::UserRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        self.inner.create_user(user).await
    }
    async fn get_user_by_id(
        &self,
        user_id: &str,
    ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        self.inner.get_user_by_id(user_id).await
    }
    async fn get_user_by_username(
        &self,
        username: &str,
    ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        self.inner.get_user_by_username(username).await
    }
    async fn list_users(
        &self,
    ) -> Result<Vec<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        self.inner.list_users().await
    }
    async fn update_user_password(
        &self,
        user_id: &str,
        password_hash: &str,
        must_change_password: bool,
    ) -> Result<bool, agentos_core::types::StorageError> {
        self.inner
            .update_user_password(user_id, password_hash, must_change_password)
            .await
    }
    async fn update_last_login(
        &self,
        user_id: &str,
    ) -> Result<(), agentos_core::types::StorageError> {
        self.inner.update_last_login(user_id).await
    }
    async fn delete_user(&self, user_id: &str) -> Result<bool, agentos_core::types::StorageError> {
        self.inner.delete_user(user_id).await
    }
}

fn probe_state(store: std::sync::Arc<PendingProbeStore>) -> AppState {
    let mut state = AppState::new();
    let dyn_store: std::sync::Arc<dyn StorageBackend> = store;
    state.store = Some(dyn_store);
    state
}

fn probe_record(id: &str) -> PendingInputRecord {
    PendingInputRecord {
        id: id.to_string(),
        pipeline_id: "pipe-fail-del".to_string(),
        tenant_id: "default".to_string(),
        user_id: "u1".to_string(),
        content: "x".to_string(),
        thread: "thread-probe".to_string(),
        source: PendingInputSource::Trigger,
        agent_id: "agentos".to_string(),
        route_id: "pipe-fail-del".to_string(),
        thinking_strength: String::new(),
        client_message_id: String::new(),
        execution_context: None,
        state_overlay: None,
        pipeline_config_id: None,
        created_at: chrono::Utc::now().to_rfc3339(),
    }
}

#[tokio::test]
async fn delete_with_failing_list_returns_503_and_keeps_entry() {
    let probe = std::sync::Arc::new(PendingProbeStore::new());
    probe.seed("pipe-fail-del", "pi_del_1", "").await;
    probe
        .fail_list
        .store(true, std::sync::atomic::Ordering::SeqCst);
    let state = probe_state(probe.clone());

    let resp = pending_inputs_delete_handler(
        axum::extract::State(state),
        axum::extract::Path(("pipe-fail-del".to_string(), "pi_del_1".to_string())),
        HeaderMap::new(),
    )
    .await;

    match resp {
        Ok(body) => panic!("list 故障时不得照常删除伪装成功，实际 {:?}", body.0),
        Err(e) => assert!(
            matches!(e, ApiError::ServiceUnavailable { .. }),
            "应返回 503，实际 {e:?}"
        ),
    }
    probe
        .fail_list
        .store(false, std::sync::atomic::Ordering::SeqCst);
    assert_eq!(
        probe.listed("pipe-fail-del").await.len(),
        1,
        "队列条目必须保留——消费循环稍后仍可正常执行并回传 outcome"
    );
}

#[tokio::test]
async fn clear_with_failing_list_returns_503_and_keeps_entries() {
    let probe = std::sync::Arc::new(PendingProbeStore::new());
    StorageBackend::enqueue_pending_input(
        probe.inner.as_ref(),
        "default",
        "pipe-fail-clr",
        &probe_record("pi_c_1"),
    )
    .await
    .unwrap();
    StorageBackend::enqueue_pending_input(
        probe.inner.as_ref(),
        "default",
        "pipe-fail-clr",
        &probe_record("pi_c_2"),
    )
    .await
    .unwrap();
    probe
        .fail_list
        .store(true, std::sync::atomic::Ordering::SeqCst);
    let state = probe_state(probe.clone());

    let resp = pending_inputs_clear_handler(
        axum::extract::State(state),
        axum::extract::Path("pipe-fail-clr".to_string()),
        HeaderMap::new(),
    )
    .await;

    match resp {
        Ok(body) => panic!("list 故障时不得照常清空伪装成功，实际 {:?}", body.0),
        Err(e) => assert!(
            matches!(e, ApiError::ServiceUnavailable { .. }),
            "应返回 503，实际 {e:?}"
        ),
    }
    probe
        .fail_list
        .store(false, std::sync::atomic::Ordering::SeqCst);
    assert_eq!(
        probe.listed("pipe-fail-clr").await.len(),
        2,
        "清空失败不得破坏排队条目"
    );
}

#[tokio::test]
async fn delete_with_healthy_list_notifies_rest_waiter() {
    // 正向对照：list 正常 → 删除成功 + REST chat waiter 收到失败 outcome
    // （这正是故障分支要保护的那条通知链）。
    let probe = std::sync::Arc::new(PendingProbeStore::new());
    probe
        .seed("pipe-fail-del", "pi_del_ok", "http_probe_notify_1")
        .await;
    let state = probe_state(probe);

    let (tx, rx) = tokio::sync::oneshot::channel();
    crate::ws_session::register_outcome_waiter("http_probe_notify_1".to_string(), tx);

    let resp = pending_inputs_delete_handler(
        axum::extract::State(state),
        axum::extract::Path(("pipe-fail-del".to_string(), "pi_del_ok".to_string())),
        HeaderMap::new(),
    )
    .await;

    assert!(resp.is_ok(), "list 正常时删除应成功");
    let outcome = tokio::time::timeout(std::time::Duration::from_secs(2), rx)
        .await
        .expect("waiter 应被通知")
        .expect("outcome 通道不应关闭");
    assert!(outcome.failed, "逐出的排队条目应对 REST 请求回失败 outcome");
}
