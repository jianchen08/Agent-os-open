// @feature: FP-0.2.可观测性 锚点活性探测面(执行路径身份统一批次同落) | @ci: rust-test
//! 锚点活性守卫（刀4，ADR 草稿 docs/working/2026-10-15-session-anchor-lifecycle-
//! adr-draft.md 推荐态落码）：锚点驱动投递（POST /api/v1/chat）在消费时刻同步
//! 校验锚点管线活性——内核簿记键 `run_status` 命中终态（completed/failed/
//! cancelled）且主管线（pipeline_ids[0]）存在且非锚点本身 → 惰性切回主管线并
//! 投主管线。判定只读内核自有键，零任务域知识；活跃/挂起/无簿记保持锚点
//! （任务对话续投与 roleplay 挂起场景是合法语义）；簿记读失败降级按旧锚点投递。

use agentos_core::traits::StorageBackend;
use agentos_core::types::{SessionRecord, TenantContext};
use std::sync::Arc;

/// 锚点活性裁决（纯函数，IO 无关）：`run_status` 簿记读取结果（Err = 簿记读
/// 失败，降级输入）× 主管线在场性 × 锚点本身 → Keep（保持锚点）或
/// ReleaseTo（切回主管线）。
#[derive(Debug, PartialEq, Eq)]
enum AnchorDecision {
    Keep,
    ReleaseTo(String),
}

fn anchor_decision(
    anchor: &str,
    main: Option<&str>,
    run_status: Result<Option<String>, String>,
) -> AnchorDecision {
    // 主管线缺席（pipeline_ids 空）或锚点即主管线（纯聊天会话常态）：无从切回
    // 或无需切回，保持锚点——纯聊天管线上逐轮 completed 是会话常态，不是矛盾态。
    let Some(main_pid) = main else {
        return AnchorDecision::Keep;
    };
    if main_pid == anchor {
        return AnchorDecision::Keep;
    }
    // 簿记读失败：守卫降级（路由纠偏是治理层，不升级为聊天不可用），保持锚点。
    let Ok(Some(status)) = run_status else {
        return AnchorDecision::Keep;
    };
    match status.as_str() {
        // 终态 → 释放锚点回主管线；挂起/运行中/无簿记（未跑过的登记管道）保持
        // ——挂起任务等交互续投、活跃任务对话续投是合法语义（R154 主收益）。
        "completed" | "failed" | "cancelled" => AnchorDecision::ReleaseTo(main_pid.to_string()),
        _ => AnchorDecision::Keep,
    }
}

/// REST chat 锚点解析 + 活性守卫（chat_handler 接线）：会话在场 → 锚点守卫
/// 裁决（终态惰性切回，切回就地 update_session 落库——指针归位一次生效，后续
/// 消费零重复写）；会话缺席/无锚点 → 空 pipeline_id（调用方既有显式报错路径）。
pub(super) async fn resolve_chat_pipeline_id(
    store: &Arc<dyn StorageBackend>,
    tenant: &TenantContext,
    session: Option<SessionRecord>,
) -> String {
    let Some(mut session) = session else {
        return String::new();
    };
    let Some(anchor) = session.active_pipeline_id.clone().filter(|p| !p.is_empty()) else {
        return String::new();
    };
    let main = session.pipeline_ids.first().cloned();
    let run_status = match store.load_pipeline_state(&anchor, &tenant.tenant_id).await {
        Ok(fields) => Ok(fields
            .get("run_status")
            .and_then(|v| v.as_str())
            .map(String::from)),
        Err(e) => {
            tracing::warn!(
                thread = %session.thread_id,
                pipeline = %anchor,
                error = %e,
                "锚点活性簿记读取失败，按旧锚点投递（守卫降级）"
            );
            Err(e.to_string())
        }
    };
    let (thread_id, tenant_id) = (session.thread_id.clone(), tenant.tenant_id.clone());
    match anchor_decision(&anchor, main.as_deref(), run_status) {
        AnchorDecision::Keep => anchor,
        AnchorDecision::ReleaseTo(main_pid) => {
            // 指针仍指终态管线才动（裁决含 main != anchor 前置）；写失败不阻断
            // 本次投递（按主管线路由已定，指针残留下轮重试，warn 可检索）。
            session.active_pipeline_id = Some(main_pid.clone());
            session.updated_at = chrono::Utc::now().to_rfc3339();
            // 簿记写面（锚指针落库 store.update_session）：scope 内无反调
            // 派发、无身份消费点，两参 `new` 不丢执行者身份；执行/反调路径
            // 才须 `TenantContext::inherit`（A1 统一收口契约）。
            let scope_tenant = TenantContext::new(tenant_id, thread_id.clone());
            let store = store.clone();
            let sess = session.clone();
            if let Err(e) =
                agentos_tenant::scope(
                    scope_tenant,
                    async move { store.update_session(&sess).await },
                )
                .await
            {
                tracing::warn!(
                    thread = %thread_id,
                    pipeline = %anchor,
                    error = %e,
                    "终态锚点切回落库失败，本次按主管线投递（指针残留下轮重试）"
                );
            }
            tracing::info!(
                thread = %thread_id,
                from = %anchor,
                to = %main_pid,
                "终态锚点惰性切回主管线（刀4 守卫）"
            );
            main_pid
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_core::traits::{MessageQueryOpts, SessionListFilter};
    use agentos_core::types::{
        MessageRecord, PipelineRunInfo, RunRecord, SegmentRecord, StorageError, TraceEntry,
        UserRecord,
    };

    fn session_record(thread: &str, anchor: Option<&str>, mains: &[&str]) -> SessionRecord {
        SessionRecord {
            thread_id: thread.to_string(),
            title: None,
            intent: None,
            current_state: "active".to_string(),
            agent_id: None,
            active_pipeline_id: anchor.map(String::from),
            pipeline_ids: mains.iter().map(|s| s.to_string()).collect(),
            metadata: None,
            created_at: "2026-10-15T00:00:00Z".to_string(),
            updated_at: "2026-10-15T00:00:00Z".to_string(),
            last_active_at: None,
        }
    }

    // ── 裁决矩阵（纯函数，IO 无关穷举）─────────────────────────────────

    #[test]
    fn decision_matrix() {
        // 终态三署名 → 切回主管线。
        for status in ["completed", "failed", "cancelled"] {
            assert_eq!(
                anchor_decision("p_task", Some("p_main"), Ok(Some(status.to_string()))),
                AnchorDecision::ReleaseTo("p_main".to_string()),
                "终态 {status} 必须切回"
            );
        }
        // 活跃/挂起/无簿记 → 保持锚点。
        for status in ["running", "suspended"] {
            assert_eq!(
                anchor_decision("p_task", Some("p_main"), Ok(Some(status.to_string()))),
                AnchorDecision::Keep,
                "{status} 必须保持锚点"
            );
        }
        assert_eq!(
            anchor_decision("p_task", Some("p_main"), Ok(None)),
            AnchorDecision::Keep,
            "无簿记（未跑过的登记管道）保持锚点"
        );
        // 簿记读失败 → 降级保持锚点（路由纠偏不升级为聊天不可用）。
        assert_eq!(
            anchor_decision("p_task", Some("p_main"), Err("db down".to_string())),
            AnchorDecision::Keep,
            "簿记故障降级保持锚点"
        );
        // 主管线缺席 / 锚点即主管线（纯聊天会话，逐轮 completed 是常态）→ 保持。
        assert_eq!(
            anchor_decision("p_task", None, Ok(Some("completed".to_string()))),
            AnchorDecision::Keep,
            "无主管线无从切回"
        );
        assert_eq!(
            anchor_decision("p_main", Some("p_main"), Ok(Some("completed".to_string()))),
            AnchorDecision::Keep,
            "锚点即主管线无需切回"
        );
    }

    // ── 故障注入存储（只覆守卫触点：load_pipeline_state / update_session）──

    #[derive(Default)]
    struct GuardFaultStore {
        load_err: bool,
        load_status: Option<String>,
        update_err: bool,
    }

    impl GuardFaultStore {
        fn state_result(
            &self,
        ) -> Result<std::collections::HashMap<String, serde_json::Value>, StorageError> {
            if self.load_err {
                return Err(StorageError::Database("injected load failure".to_string()));
            }
            let mut m = std::collections::HashMap::new();
            if let Some(s) = &self.load_status {
                m.insert("run_status".to_string(), serde_json::json!(s));
            }
            Ok(m)
        }
    }

    #[async_trait::async_trait]
    impl StorageBackend for GuardFaultStore {
        async fn get_run(&self, _run_id: &str) -> Result<RunRecord, StorageError> {
            unreachable!("守卫不触碰")
        }
        async fn get_messages_by_pipeline(
            &self,
            _pipeline_id: &str,
            _opts: MessageQueryOpts,
        ) -> Result<Vec<MessageRecord>, StorageError> {
            unreachable!("守卫不触碰")
        }
        async fn get_blob(&self, _blob_id: &str) -> Result<Vec<u8>, StorageError> {
            unreachable!("守卫不触碰")
        }
        async fn append_trace(&self, _entry: TraceEntry) -> Result<(), StorageError> {
            unreachable!("守卫不触碰")
        }
        async fn store_blob(&self, _data: &[u8], _mime: &str) -> Result<String, StorageError> {
            unreachable!("守卫不触碰")
        }
        async fn create_session(&self, _session: &SessionRecord) -> Result<(), StorageError> {
            unreachable!("守卫不触碰")
        }
        async fn get_session(
            &self,
            _thread_id: &str,
        ) -> Result<Option<SessionRecord>, StorageError> {
            unreachable!("守卫不触碰")
        }
        async fn list_sessions(
            &self,
            _filter: SessionListFilter,
        ) -> Result<Vec<SessionRecord>, StorageError> {
            unreachable!("守卫不触碰")
        }
        async fn update_session(&self, _session: &SessionRecord) -> Result<(), StorageError> {
            if self.update_err {
                return Err(StorageError::Database(
                    "injected update failure".to_string(),
                ));
            }
            Ok(())
        }
        async fn delete_session(&self, _thread_id: &str) -> Result<Vec<String>, StorageError> {
            unreachable!("守卫不触碰")
        }
        async fn link_pipeline_session(
            &self,
            _pipeline_id: &str,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<(), StorageError> {
            unreachable!("守卫不触碰")
        }
        async fn list_pipeline_ids_by_thread(
            &self,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<String>, StorageError> {
            unreachable!("守卫不触碰")
        }
        async fn get_step_traces_by_thread(
            &self,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<TraceEntry>, StorageError> {
            unreachable!("守卫不触碰")
        }
        async fn create_user(&self, _user: &UserRecord) -> Result<(), StorageError> {
            unreachable!("守卫不触碰")
        }
        async fn get_user_by_id(&self, _user_id: &str) -> Result<Option<UserRecord>, StorageError> {
            unreachable!("守卫不触碰")
        }
        async fn get_user_by_username(
            &self,
            _username: &str,
        ) -> Result<Option<UserRecord>, StorageError> {
            unreachable!("守卫不触碰")
        }
        async fn list_users(&self) -> Result<Vec<UserRecord>, StorageError> {
            unreachable!("守卫不触碰")
        }
        async fn update_last_login(&self, _user_id: &str) -> Result<(), StorageError> {
            unreachable!("守卫不触碰")
        }
        async fn update_user_password(
            &self,
            _user_id: &str,
            _password_hash: &str,
            _must_change_password: bool,
        ) -> Result<bool, StorageError> {
            unreachable!("守卫不触碰")
        }
        async fn delete_user(&self, _user_id: &str) -> Result<bool, StorageError> {
            unreachable!("守卫不触碰")
        }
        async fn list_pipelines(
            &self,
            _tenant_id: &str,
            _status: Option<&str>,
            _limit: u32,
        ) -> Result<Vec<PipelineRunInfo>, StorageError> {
            unreachable!("守卫不触碰")
        }
        async fn get_message_segment(
            &self,
            _segment_id: &str,
            _tenant_id: &str,
        ) -> Result<Option<SegmentRecord>, StorageError> {
            unreachable!("守卫不触碰")
        }
        async fn load_pipeline_state(
            &self,
            _pipeline_id: &str,
            _tenant_id: &str,
        ) -> Result<std::collections::HashMap<String, serde_json::Value>, StorageError> {
            self.state_result()
        }
    }

    fn tenant() -> TenantContext {
        TenantContext::new("t1".to_string(), "kernel".to_string())
    }

    #[tokio::test]
    async fn bookkeeping_failure_degrades_to_anchor() {
        let store: Arc<dyn StorageBackend> = Arc::new(GuardFaultStore {
            load_err: true,
            ..Default::default()
        });
        let s = session_record("thread-g1", Some("p_task"), &["p_main"]);
        let out = resolve_chat_pipeline_id(&store, &tenant(), Some(s)).await;
        assert_eq!(out, "p_task", "簿记故障降级：按旧锚点投递");
    }

    #[tokio::test]
    async fn terminal_anchor_releases_to_main_via_mock() {
        let store: Arc<dyn StorageBackend> = Arc::new(GuardFaultStore {
            load_status: Some("completed".to_string()),
            ..Default::default()
        });
        let s = session_record("thread-g1", Some("p_task"), &["p_main"]);
        let out = resolve_chat_pipeline_id(&store, &tenant(), Some(s)).await;
        assert_eq!(out, "p_main", "终态锚点切回主管线投递");
    }

    #[tokio::test]
    async fn release_write_failure_still_routes_main() {
        let store: Arc<dyn StorageBackend> = Arc::new(GuardFaultStore {
            load_status: Some("failed".to_string()),
            update_err: true,
            ..Default::default()
        });
        let s = session_record("thread-g1", Some("p_task"), &["p_main"]);
        let out = resolve_chat_pipeline_id(&store, &tenant(), Some(s)).await;
        assert_eq!(out, "p_main", "切回落库失败不阻断本次投递（warn 留痕）");
    }

    #[tokio::test]
    async fn session_absent_returns_empty_coordinate() {
        let store: Arc<dyn StorageBackend> = Arc::new(GuardFaultStore::default());
        let out = resolve_chat_pipeline_id(&store, &tenant(), None).await;
        assert_eq!(out, String::new(), "会话缺席 → 空坐标走既有显式报错路径");
    }

    use tower::ServiceExt;

    // ── HTTP 全链（POST /api/v1/chat = BUG-45 实锤投递面）──────────────

    const E2E_PW: &str = "test-admin-pw-2026";

    async fn seed_admin_and_session(
        sqlite: &std::sync::Arc<agentos_engine::SqliteStore>,
        thread: &str,
        anchor: &str,
        main: &str,
    ) {
        let admin = agentos_core::types::UserRecord {
            user_id: "00000000-0000-0000-0000-000000000001".to_string(),
            username: "admin".to_string(),
            password: agentos_http::auth::hash_password(E2E_PW).unwrap(),
            email: Some("admin@agentos.dev".to_string()),
            role: "admin".to_string(),
            tenant_id: "default".to_string(),
            created_at: "2026-08-30T00:00:00Z".to_string(),
            last_login_at: None,
            must_change_password: false,
        };
        let _ = StorageBackend::create_user(sqlite.as_ref(), &admin).await;
        sqlite
            .create_session(&session_record(thread, Some(anchor), &[main]))
            .await
            .unwrap();
        sqlite
            .link_pipeline_session(anchor, thread, "default")
            .await
            .unwrap();
        sqlite
            .link_pipeline_session(main, thread, "default")
            .await
            .unwrap();
    }

    async fn login_token(app: &axum::Router) -> String {
        let resp = app
            .clone()
            .oneshot(
                axum::http::Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/login")
                    .header("content-type", "application/json")
                    .body(axum::body::Body::from(
                        serde_json::json!({"username": "admin", "password": E2E_PW}).to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        v["access_token"].as_str().unwrap().to_string()
    }

    async fn post_chat(app: &axum::Router, token: &str, thread: &str) -> serde_json::Value {
        let resp = app
            .clone()
            .oneshot(
                axum::http::Request::builder()
                    .method("POST")
                    .uri("/api/v1/chat")
                    .header("authorization", format!("Bearer {token}"))
                    .header("content-type", "application/json")
                    .body(axum::body::Body::from(
                        serde_json::json!({"message": "继续", "session_id": thread}).to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), axum::http::StatusCode::OK);
        let body = axum::body::to_bytes(resp.into_body(), 65536).await.unwrap();
        serde_json::from_slice(&body).unwrap()
    }

    /// BUG-45 实锤面回归锁：锚点指向终态任务管线时，POST /chat 自动切回主管线
    /// 投递且锚点就地归位——终态管线不再收新输入（dropped_tool_calls 源头消除）。
    #[tokio::test]
    async fn http_chat_on_terminal_anchor_switches_back_to_main() {
        let (state, _invoker, _store, sqlite, _us) = crate::server::tests::make_engine_state();
        seed_admin_and_session(&sqlite, "thread-g2", "p_task_dead", "p_main").await;
        sqlite
            .upsert_state_field(
                "p_task_dead",
                "default",
                "run_status",
                &serde_json::json!("completed"),
            )
            .unwrap();
        let app = crate::server::build_router(state);
        let token = login_token(&app).await;
        let resp = post_chat(&app, &token, "thread-g2").await;
        assert_eq!(resp["type"], "message");
        let sess = StorageBackend::get_session(sqlite.as_ref(), "thread-g2")
            .await
            .unwrap()
            .expect("会话在场");
        assert_eq!(
            sess.active_pipeline_id.as_deref(),
            Some("p_main"),
            "终态锚点必须就地归位主管线"
        );
    }

    /// 活跃任务保持：run_status=running 的锚点不切回（任务对话续投语义不变）。
    #[tokio::test]
    async fn http_chat_on_running_anchor_keeps_task_pipeline() {
        let (state, _invoker, _store, sqlite, _us) = crate::server::tests::make_engine_state();
        seed_admin_and_session(&sqlite, "thread-g3", "p_task_live", "p_main").await;
        sqlite
            .upsert_state_field(
                "p_task_live",
                "default",
                "run_status",
                &serde_json::json!("running"),
            )
            .unwrap();
        let app = crate::server::build_router(state);
        let token = login_token(&app).await;
        let _ = post_chat(&app, &token, "thread-g3").await;
        let sess = StorageBackend::get_session(sqlite.as_ref(), "thread-g3")
            .await
            .unwrap()
            .expect("会话在场");
        assert_eq!(
            sess.active_pipeline_id.as_deref(),
            Some("p_task_live"),
            "活跃任务锚点保持，消息续投任务管线"
        );
    }
}
