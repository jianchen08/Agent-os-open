// @feature: FP-0.2.一 多用户会话域路由 | @ci: rust-test
//! session_routes 域测试全集——由 session_routes.rs 的测试模块体平移而来
//! （保留父模块私有项访问：use super::*；文件长度机械闸触碰即拆，
//! ADR 2026-10-12-file-length-mechanical-gate 同款 capability_router/tests.rs
//! 与 chat_send_handler/tests.rs 先例）。

use super::*;

#[cfg(test)]
mod agent_binding_tests {
    //! 会话 agent 绑定三写集成测试（2026-08-24 阶段1：绑定真值落管道 state）。
    //!
    //! 设计依据：docs/working/管道配置输入契约与动态管道能力设计_20260824.md
    //! §4.3——PATCH /sessions/{id}/agent 必须三写：registry 线程绑定（热路径
    //! 消费）+ sessions 表 agent_id（冷路径 DB 兜底）+ 主管道 state `agent.id`
    //! （持久化真值）。执行面解析（显式 → registry → DB → agentos）由
    //! ws_session::resolve_dispatch_agent 单测覆盖，本模块验证写入面三方落齐。

    use agentos_core::traits::StorageBackend;

    use super::*;

    const SEED_ADMIN_PW: &str = "test-admin-pw-2026";
    use axum::{body::Body, http::Request};
    use tower::ServiceExt;

    const ADMIN_USER_ID: &str = "00000000-0000-0000-0000-000000000001";

    async fn setup() -> (
        axum::Router,
        AppState,
        std::sync::Arc<agentos_engine::SqliteStore>,
    ) {
        let store = std::sync::Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        store
            .create_user(&UserRecord {
                user_id: ADMIN_USER_ID.to_string(),
                username: "admin".to_string(),
                password: agentos_http::auth::hash_password(SEED_ADMIN_PW).unwrap(),
                email: Some("admin@agentos.dev".to_string()),
                role: "admin".to_string(),
                tenant_id: "default".to_string(),
                created_at: chrono::Utc::now().to_rfc3339(),
                last_login_at: None,
                must_change_password: false,
            })
            .await
            .unwrap();
        let mut state = AppState::new();
        state.store = Some(store.clone());
        state.db = Some(store.clone());
        state.session = Some(std::sync::Arc::new(
            agentos_session::SessionCoordinator::new(),
        ));
        (crate::server::build_router(state.clone()), state, store)
    }

    async fn admin_token(router: &axum::Router) -> String {
        let resp = router
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/login")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        json!({"username": "admin", "password": SEED_ADMIN_PW}).to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let json: Value = serde_json::from_slice(&body).unwrap();
        json["access_token"].as_str().unwrap().to_string()
    }

    async fn create_and_patch_agent(
        router: &axum::Router,
        state: &AppState,
        token: &str,
        create_agent: Option<&str>,
        patch_agent: &str,
    ) -> (String, String) {
        let body = match create_agent {
            Some(a) => json!({"agent_id": a, "intent": "t"}),
            None => json!({"intent": "t"}),
        };
        let create = router
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/sessions")
                    .header("content-type", "application/json")
                    .header("authorization", format!("Bearer {token}"))
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(create.status(), 200, "create_session 应成功");
        let created: Value = serde_json::from_slice(
            &axum::body::to_bytes(create.into_body(), 8192)
                .await
                .unwrap(),
        )
        .unwrap();
        let thread_id = created["thread_id"].as_str().unwrap().to_string();
        let pipeline_id = created["active_pipeline_id"]
            .as_str()
            .unwrap_or_else(|| created["pipeline_ids"][0].as_str().unwrap())
            .to_string();
        // 补注册 pipeline↔thread 映射（生产路径 F8 link 已落，here 同步 registry）
        let registry = state.session.as_ref().unwrap().registry().clone();
        registry.register_thread(&thread_id, ADMIN_USER_ID);
        registry.register_thread_pipeline(&thread_id, &pipeline_id);

        let patch = router
            .clone()
            .oneshot(
                Request::builder()
                    .method("PATCH")
                    .uri(format!("/api/v1/sessions/{thread_id}/agent"))
                    .header("content-type", "application/json")
                    .header("authorization", format!("Bearer {token}"))
                    .body(Body::from(json!({"agent_id": patch_agent}).to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(patch.status(), 200, "PATCH agent 应成功");
        let patched: Value =
            serde_json::from_slice(&axum::body::to_bytes(patch.into_body(), 8192).await.unwrap())
                .unwrap();
        assert_eq!(patched["agent_id"].as_str(), Some(patch_agent));
        (thread_id, pipeline_id)
    }

    #[tokio::test]
    async fn create_then_patch_agent_writes_registry_db_and_pipeline_state() {
        let (router, state, store) = setup().await;
        let token = admin_token(&router).await;
        let (thread_id, pipeline_id) =
            create_and_patch_agent(&router, &state, &token, None, "general_agent").await;

        // ① registry 热绑定
        let registry = state.session.as_ref().unwrap().registry();
        assert_eq!(
            registry.get_agent_for_thread(&thread_id).as_deref(),
            Some("general_agent"),
            "registry 线程绑定必须同步"
        );

        // ② sessions 表冷兜底
        let session = store.get_session(&thread_id).await.unwrap().unwrap();
        assert_eq!(
            session.agent_id.as_deref(),
            Some("general_agent"),
            "sessions 表 agent_id 必须同步"
        );

        // ③ 主管道 state 持久化真值（阶段 1 核心断言）
        let store_obj: std::sync::Arc<dyn StorageBackend> = store.clone();
        let tenant_id =
            agentos_http::auth::resolve_tenant_id_by_user(Some(&store_obj), ADMIN_USER_ID)
                .await
                .expect("admin 播种在库，解析必须成功");
        let state_fields = store_obj
            .load_pipeline_state(&pipeline_id, &tenant_id)
            .await
            .unwrap();
        assert_eq!(
            state_fields.get("agent.id").and_then(|v| v.as_str()),
            Some("general_agent"),
            "主管道 state agent.id 必须落库（绑定真值进管道 state）"
        );
    }

    #[tokio::test]
    async fn create_session_seeds_agent_id_into_pipeline_state() {
        // 创建时即选 agent → 出生绑定直接落 state（不必等 PATCH）
        let (router, state, store) = setup().await;
        let token = admin_token(&router).await;
        let (_, pipeline_id) = create_and_patch_agent(
            &router,
            &state,
            &token,
            Some("general_agent"),
            "general_agent",
        )
        .await;

        let store_obj: std::sync::Arc<dyn StorageBackend> = store.clone();
        let tenant_id =
            agentos_http::auth::resolve_tenant_id_by_user(Some(&store_obj), ADMIN_USER_ID)
                .await
                .expect("admin 播种在库，解析必须成功");
        let state_fields = store_obj
            .load_pipeline_state(&pipeline_id, &tenant_id)
            .await
            .unwrap();
        assert_eq!(
            state_fields.get("agent.id").and_then(|v| v.as_str()),
            Some("general_agent"),
            "出生绑定（创建时指定的 agent_id）必须落管道 state"
        );
    }
}

#[cfg(test)]
mod messages_query_tests {
    //! K3：历史消息查询失败 → ApiError::ServiceUnavailable（503），不再伪装成
    //! 200 空数组（前端无法区分「新会话无历史」与「查询挂了」）。

    use super::*;

    #[test]
    fn map_messages_query_failure_yields_service_unavailable() {
        let e = map_messages_query_failure(
            "pipe-1",
            StorageError::Database("connection lost".to_string()),
        );
        match e {
            ApiError::ServiceUnavailable { message } => {
                assert!(!message.is_empty(), "对外文案非空（前端渲染错误态）");
            }
            other => panic!("DB 查询失败应映射 503，实际 {other:?}"),
        }
    }

    #[test]
    fn map_messages_query_failure_covers_all_storage_error_kinds() {
        // 各类存储错误（NotFound 之外的故障形态）都不得回落 200 空数组
        for e in [
            StorageError::Database("db".to_string()),
            StorageError::Io("io".to_string()),
            StorageError::Serialization("ser".to_string()),
        ] {
            assert!(matches!(
                map_messages_query_failure("p", e),
                ApiError::ServiceUnavailable { .. }
            ));
        }
    }
}

#[cfg(test)]
mod sessions_list_tests {
    //! 扫描 2026-08-27 辖区一 Should#6：list_sessions DB 故障 → 503，不再回退
    //! 内存 registry 把「存储挂了」伪装成 200 陈旧列表（假成功）；store 存在即
    //! 权威——DB 空 = 空列表（清空执行数据后不回退内存 registry 出口未命名幽灵，
    //! 2026-08-30 修正），仅 store 未配置（无持久化数据源）时回退内存 registry
    //! （显式兼容语义非吞错）。

    use agentos_core::traits::StorageBackend;
    use axum::extract::State;

    use super::*;

    /// list_sessions 恒故障的存储 mock。其余必需方法以 unreachable! 桩实现——
    /// 错误路径若意外耦合了其他存储调用会在此炸出（比静默 NotFound 更诚实）。
    struct ErrListStore;

    #[async_trait::async_trait]
    impl StorageBackend for ErrListStore {
        async fn list_sessions(
            &self,
            _filter: SessionListFilter,
        ) -> Result<Vec<SessionRecord>, StorageError> {
            Err(StorageError::Database(
                "injected sessions list failure".to_string(),
            ))
        }
        async fn get_run(&self, _run_id: &str) -> Result<RunRecord, StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn get_messages_by_pipeline(
            &self,
            _pipeline_id: &str,
            _opts: MessageQueryOpts,
        ) -> Result<Vec<MessageRecord>, StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn get_blob(&self, _blob_id: &str) -> Result<Vec<u8>, StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn append_trace(&self, _entry: TraceEntry) -> Result<(), StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn store_blob(&self, _data: &[u8], _mime_type: &str) -> Result<String, StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn create_session(&self, _session: &SessionRecord) -> Result<(), StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn get_session(
            &self,
            _thread_id: &str,
        ) -> Result<Option<SessionRecord>, StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn update_session(&self, _session: &SessionRecord) -> Result<(), StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn delete_session(&self, _thread_id: &str) -> Result<Vec<String>, StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn link_pipeline_session(
            &self,
            _pipeline_id: &str,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<(), StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn list_pipeline_ids_by_thread(
            &self,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<String>, StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn get_step_traces_by_thread(
            &self,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<TraceEntry>, StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn create_user(&self, _user: &UserRecord) -> Result<(), StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn get_user_by_id(&self, _user_id: &str) -> Result<Option<UserRecord>, StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn get_user_by_username(
            &self,
            _username: &str,
        ) -> Result<Option<UserRecord>, StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn list_users(&self) -> Result<Vec<UserRecord>, StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn update_last_login(&self, _user_id: &str) -> Result<(), StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn update_user_password(
            &self,
            _user_id: &str,
            _password_hash: &str,
            _must_change_password: bool,
        ) -> Result<bool, StorageError> {
            unreachable!("mock 不提供口令更新")
        }
        async fn delete_user(&self, _user_id: &str) -> Result<bool, StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
    }

    #[tokio::test]
    async fn db_error_returns_503_not_stale_registry_list() {
        // registry 预置陈旧线程：修复前该数据会以 200「成功」出口。
        let mut state = AppState::new();
        state.store = Some(std::sync::Arc::new(ErrListStore) as std::sync::Arc<dyn StorageBackend>);
        state.session = Some(std::sync::Arc::new(
            agentos_session::SessionCoordinator::new(),
        ));
        state
            .session
            .as_ref()
            .unwrap()
            .registry()
            .register_thread("thread-stale-1", "u1");

        let resp = list_sessions_handler(State(state), HeaderMap::new()).await;

        match resp {
            Ok(body) => panic!("DB 故障不得回退内存 registry 假成功，实际 {:?}", body.0),
            Err(e) => assert!(
                matches!(e, ApiError::ServiceUnavailable { .. }),
                "DB 故障应映射 503，实际 {e:?}"
            ),
        }
    }

    #[tokio::test]
    async fn empty_db_returns_empty_list_with_200() {
        // DB 正常但空 = 无会话（store 存在即权威，不回退内存 registry 出口
        // title=null 幽灵——清空执行数据后 sessions 表清空即此场景）。
        let store = std::sync::Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let mut state = AppState::new();
        state.store = Some(store as std::sync::Arc<dyn StorageBackend>);
        state.session = Some(std::sync::Arc::new(
            agentos_session::SessionCoordinator::new(),
        ));
        state
            .session
            .as_ref()
            .unwrap()
            .registry()
            .register_thread("thread-mem-1", "u1");

        let resp = list_sessions_handler(State(state), HeaderMap::new())
            .await
            .expect("DB 空属正常场景应 200");

        assert_eq!(resp.0["total"], 0, "DB 空不得回退内存 registry: {}", resp.0);
        assert!(
            resp.0["threads"].as_array().unwrap().is_empty(),
            "threads 应为空: {}",
            resp.0
        );
    }

    #[test]
    fn map_sessions_list_failure_covers_storage_error_kinds() {
        for e in [
            StorageError::Database("db".to_string()),
            StorageError::Io("io".to_string()),
            StorageError::Serialization("ser".to_string()),
        ] {
            assert!(matches!(
                map_sessions_list_failure(e),
                ApiError::ServiceUnavailable { .. }
            ));
        }
    }
}

#[cfg(test)]
mod transient_states_query_tests {
    //! ADR 2026-08-27 §2.6：消息读取接口带出存活中间态（前端 F5 刷新恢复）。
    //! 纯增字段向后兼容：寄存器无该管道条目时为空数组，不改变既有响应形状。

    use super::*;

    #[tokio::test]
    async fn messages_response_carries_live_transient_states() {
        let store = std::sync::Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let mut state = AppState::new();
        state.store = Some(store.clone());
        state.db = Some(store.clone());
        let reg = agentos_engine::global_registry();
        let pipe = "pipe_transient_msg";
        reg.set("default", pipe, "chunk:a_abc", json!({"text_len": 12}));
        reg.set("default", pipe, "progress:1", json!({"pct": 40}));

        let resp = list_session_messages_handler(
            State(state),
            HeaderMap::new(),
            Path(pipe.to_string()),
            Query(MessageListQuery {
                pipeline_run_id: Some(pipe.to_string()),
                before_sequence: None,
                after_sequence: None,
                limit: None,
            }),
        )
        .await
        .unwrap();
        let body = resp.0;
        assert_eq!(body["total"], json!(0), "无历史消息");
        assert_eq!(body["has_more"], json!(false));
        // 存活中间态带出（key/value 对，供前端重建流式占位）
        let states = body["transient_states"].as_array().unwrap();
        assert_eq!(states.len(), 2);
        let keys: Vec<&str> = states.iter().filter_map(|s| s["key"].as_str()).collect();
        assert!(keys.contains(&"chunk:a_abc"));
        assert!(keys.contains(&"progress:1"));
        let chunk = states.iter().find(|s| s["key"] == "chunk:a_abc").unwrap();
        assert_eq!(chunk["value"]["text_len"], json!(12));
        reg.clear_pipeline("default", pipe);
    }

    #[tokio::test]
    async fn messages_response_empty_transient_when_none() {
        let store = std::sync::Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let mut state = AppState::new();
        state.store = Some(store.clone());
        state.db = Some(store.clone());
        // 未写任何中间态的管道 → 空数组（纯增字段向后兼容）
        let resp = list_session_messages_handler(
            State(state),
            HeaderMap::new(),
            Path("pipe_empty_transient".to_string()),
            Query(MessageListQuery {
                pipeline_run_id: Some("pipe_empty_transient".to_string()),
                before_sequence: None,
                after_sequence: None,
                limit: None,
            }),
        )
        .await
        .unwrap();
        let states = resp.0["transient_states"].as_array().unwrap();
        assert!(states.is_empty(), "无中间态时为空数组而非缺字段");
    }
}

#[cfg(test)]
mod delete_session_tests {
    //! B1：删除会话落库失败 → 503 统一错误信封，不再 `deleted:true` 假成功；
    //! `session.deleted` 域事件只在删除成立的 Ok 路径发出。

    use std::{
        collections::HashSet,
        sync::{Arc, Mutex},
    };

    use agentos_core::{
        traits::{
            HookContext, HostType, LifecycleHook, ManifestCapabilities, PluginInvoker,
            PluginManifest, PluginType, StorageBackend,
        },
        types::{PluginContext, PluginError, PluginResult, ToolExecutionResult},
    };

    use super::*;
    use crate::routes::AppState;

    /// delete_session 恒故障的存储 mock。其余必需方法以 unreachable! 桩实现——
    /// 错误路径若意外耦合其他存储调用会在此炸出（比静默成功更诚实）。
    struct ErrDeleteStore;

    #[async_trait::async_trait]
    impl StorageBackend for ErrDeleteStore {
        async fn delete_session(&self, _thread_id: &str) -> Result<Vec<String>, StorageError> {
            Err(StorageError::Database(
                "injected delete_session failure".to_string(),
            ))
        }
        async fn get_run(&self, _run_id: &str) -> Result<RunRecord, StorageError> {
            unreachable!("delete_session 错误路径不应触碰其他存储方法")
        }
        async fn get_messages_by_pipeline(
            &self,
            _pipeline_id: &str,
            _opts: MessageQueryOpts,
        ) -> Result<Vec<MessageRecord>, StorageError> {
            unreachable!("delete_session 错误路径不应触碰其他存储方法")
        }
        async fn get_blob(&self, _blob_id: &str) -> Result<Vec<u8>, StorageError> {
            unreachable!("delete_session 错误路径不应触碰其他存储方法")
        }
        async fn append_trace(&self, _entry: TraceEntry) -> Result<(), StorageError> {
            unreachable!("delete_session 错误路径不应触碰其他存储方法")
        }
        async fn store_blob(&self, _data: &[u8], _mime_type: &str) -> Result<String, StorageError> {
            unreachable!("delete_session 错误路径不应触碰其他存储方法")
        }
        async fn create_session(&self, _session: &SessionRecord) -> Result<(), StorageError> {
            unreachable!("delete_session 错误路径不应触碰其他存储方法")
        }
        async fn get_session(
            &self,
            _thread_id: &str,
        ) -> Result<Option<SessionRecord>, StorageError> {
            unreachable!("delete_session 错误路径不应触碰其他存储方法")
        }
        async fn list_sessions(
            &self,
            _filter: SessionListFilter,
        ) -> Result<Vec<SessionRecord>, StorageError> {
            unreachable!("delete_session 错误路径不应触碰其他存储方法")
        }
        async fn update_session(&self, _session: &SessionRecord) -> Result<(), StorageError> {
            unreachable!("delete_session 错误路径不应触碰其他存储方法")
        }
        async fn link_pipeline_session(
            &self,
            _pipeline_id: &str,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<(), StorageError> {
            unreachable!("delete_session 错误路径不应触碰其他存储方法")
        }
        async fn list_pipeline_ids_by_thread(
            &self,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<String>, StorageError> {
            unreachable!("delete_session 错误路径不应触碰其他存储方法")
        }
        async fn get_step_traces_by_thread(
            &self,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<TraceEntry>, StorageError> {
            unreachable!("delete_session 错误路径不应触碰其他存储方法")
        }
        async fn get_step_traces_by_pipeline(
            &self,
            _pipeline_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<TraceEntry>, StorageError> {
            unreachable!("delete_session 错误路径不应触碰其他存储方法")
        }
        async fn create_user(&self, _user: &UserRecord) -> Result<(), StorageError> {
            unreachable!("delete_session 错误路径不应触碰其他存储方法")
        }
        async fn get_user_by_id(&self, _user_id: &str) -> Result<Option<UserRecord>, StorageError> {
            unreachable!("delete_session 错误路径不应触碰其他存储方法")
        }
        async fn get_user_by_username(
            &self,
            _username: &str,
        ) -> Result<Option<UserRecord>, StorageError> {
            unreachable!("delete_session 错误路径不应触碰其他存储方法")
        }
        async fn list_users(&self) -> Result<Vec<UserRecord>, StorageError> {
            unreachable!("delete_session 错误路径不应触碰其他存储方法")
        }
        async fn update_last_login(&self, _user_id: &str) -> Result<(), StorageError> {
            unreachable!("delete_session 错误路径不应触碰其他存储方法")
        }
        async fn update_user_password(
            &self,
            _user_id: &str,
            _password_hash: &str,
            _must_change_password: bool,
        ) -> Result<bool, StorageError> {
            unreachable!("mock 不提供口令更新")
        }
        async fn delete_user(&self, _user_id: &str) -> Result<bool, StorageError> {
            unreachable!("delete_session 错误路径不应触碰其他存储方法")
        }
    }

    /// 记录 send_lifecycle_hook 调用的 mock invoker（invoke_* 本测试不可达）。
    struct RecordingInvoker {
        hooks: Mutex<Vec<String>>, // 事件名
    }

    #[async_trait::async_trait]
    impl PluginInvoker for RecordingInvoker {
        async fn invoke_pipeline_plugin<'a>(
            &self,
            _plugin_id: &str,
            _ctx: &PluginContext<'a>,
        ) -> Result<PluginResult, PluginError> {
            unimplemented!("域事件测试不触达")
        }
        async fn invoke_tool(
            &self,
            _plugin_id: &str,
            _tool_name: &str,
            _inputs: &serde_json::Value,
        ) -> Result<ToolExecutionResult, PluginError> {
            unimplemented!("域事件测试不触达")
        }
        async fn send_lifecycle_hook(
            &self,
            _plugin_id: &str,
            hook: LifecycleHook,
            context: &HookContext,
        ) -> Result<(), PluginError> {
            assert_eq!(hook, LifecycleHook::DomainEvent);
            let event = context
                .get("event")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            self.hooks.lock().unwrap().push(event);
            Ok(())
        }
    }

    fn domain_manifest(id: &str) -> PluginManifest {
        PluginManifest {
            force_include_tools: Vec::new(),
            state: None,
            id: id.to_string(),
            name: id.to_string(),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::System,
            language: "python".to_string(),
            host_type: HostType::Sidecar,
            host_group: None,
            entry: "python server.py".to_string(),
            capabilities: ManifestCapabilities {
                lifecycle_hooks: vec![LifecycleHook::DomainEvent],
                ..Default::default()
            },
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
            contributes: None,
            enabled: None,
            activation: None,
            persistent_fields: vec![],
            export_fields: vec![],
            aux_venvs: Vec::new(),
            provides: None,
            visibility: Default::default(),
        }
    }

    async fn wait_for_hooks(invoker: &RecordingInvoker) -> Vec<String> {
        // 点对点是 fire-and-forget spawn：轮询等任务落地
        for _ in 0..200 {
            if !invoker.hooks.lock().unwrap().is_empty() {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        std::mem::take(&mut *invoker.hooks.lock().unwrap())
    }

    #[tokio::test]
    async fn db_failure_returns_5xx_and_emits_no_event() {
        let invoker = Arc::new(RecordingInvoker {
            hooks: Mutex::new(Vec::new()),
        });
        let mut state = AppState::new();
        state.store = Some(Arc::new(ErrDeleteStore) as Arc<dyn StorageBackend>);
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![domain_manifest("p_dom")]));
        state.enabled_plugin_ids = Arc::new(tokio::sync::RwLock::new(HashSet::from([
            "p_dom".to_string()
        ])));
        state.invoker = Some(invoker.clone() as Arc<dyn PluginInvoker>);

        let resp = delete_session_handler(State(state), HeaderMap::new(), Path("t1".into())).await;

        match resp {
            Ok(body) => panic!("落库失败不得 deleted:true 假成功，实际 {:?}", body.0),
            Err(e) => {
                assert!(
                    matches!(e, ApiError::ServiceUnavailable { .. }),
                    "落库失败应映射 503，实际 {e:?}"
                );
                assert!(!e.message().is_empty(), "对外文案非空");
            }
        }
        let got = wait_for_hooks(&invoker).await;
        assert!(
            !got.iter().any(|ev| ev == "session.deleted"),
            "删除未成立不得发 session.deleted，实际 {got:?}"
        );
    }

    #[tokio::test]
    async fn success_path_returns_deleted_true_and_emits_event_once() {
        let invoker = Arc::new(RecordingInvoker {
            hooks: Mutex::new(Vec::new()),
        });
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        store
            .create_session(&SessionRecord {
                thread_id: "t-ok".to_string(),
                title: Some("s".to_string()),
                intent: None,
                current_state: "active".to_string(),
                agent_id: None,
                active_pipeline_id: None,
                pipeline_ids: vec![],
                metadata: Some(serde_json::json!({})),
                created_at: chrono::Utc::now().to_rfc3339(),
                updated_at: chrono::Utc::now().to_rfc3339(),
                last_active_at: None,
            })
            .await
            .unwrap();
        let mut state = AppState::new();
        state.store = Some(store.clone() as Arc<dyn StorageBackend>);
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![domain_manifest("p_dom")]));
        state.enabled_plugin_ids = Arc::new(tokio::sync::RwLock::new(HashSet::from([
            "p_dom".to_string()
        ])));
        state.invoker = Some(invoker.clone() as Arc<dyn PluginInvoker>);

        let resp = delete_session_handler(State(state), HeaderMap::new(), Path("t-ok".into()))
            .await
            .expect("正常删除应成功");

        assert_eq!(resp.0["deleted"], json!(true));
        assert_eq!(resp.0["thread_id"], json!("t-ok"));
        let got = wait_for_hooks(&invoker).await;
        assert_eq!(
            got,
            vec!["session.deleted".to_string()],
            "删除成立恰好发一次 session.deleted"
        );
    }

    #[tokio::test]
    async fn missing_session_is_idempotent_success() {
        // 幂等语义（会话不存在 = 成功）由 store 既有行为保留：无记录仍 200 deleted:true
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let mut state = AppState::new();
        state.store = Some(store as Arc<dyn StorageBackend>);
        let resp = delete_session_handler(
            State(state),
            HeaderMap::new(),
            Path("t-never-existed".into()),
        )
        .await
        .expect("无记录删除属幂等成功");
        assert_eq!(resp.0["deleted"], json!(true));
    }
}

#[cfg(test)]
mod sessions_crud_tests {
    //! 会话 CRUD 处理器补测：create/update 的用户来源仲裁、metadata 默认值合并、
    //! DB 故障容忍、DB 无记录 404（不伪造回退 200），以及消息查询的管道解析/分页分支。

    use std::{collections::HashMap, sync::Mutex};

    use agentos_core::traits::StorageBackend;

    use super::*;

    const ADMIN_USER_ID: &str = "00000000-0000-0000-0000-000000000001";
    pub(super) const SEED_ADMIN_PW: &str = "test-admin-pw-2026";

    fn sqlite() -> std::sync::Arc<agentos_engine::SqliteStore> {
        std::sync::Arc::new(agentos_engine::SqliteStore::open_memory().unwrap())
    }

    pub(super) fn session_record(
        thread_id: &str,
        active_pipeline_id: Option<&str>,
    ) -> SessionRecord {
        SessionRecord {
            thread_id: thread_id.to_string(),
            title: None,
            intent: None,
            current_state: "active".to_string(),
            agent_id: None,
            active_pipeline_id: active_pipeline_id.map(|s| s.to_string()),
            pipeline_ids: active_pipeline_id
                .map(|p| vec![p.to_string()])
                .unwrap_or_default(),
            metadata: None,
            created_at: "2026-09-13T00:00:00Z".to_string(),
            updated_at: "2026-09-13T00:00:00Z".to_string(),
            last_active_at: None,
        }
    }

    async fn seed_admin(store: &agentos_engine::SqliteStore) -> String {
        let password_hash = agentos_http::auth::hash_password(SEED_ADMIN_PW).unwrap();
        store
            .create_user(&UserRecord {
                user_id: ADMIN_USER_ID.to_string(),
                username: "admin".to_string(),
                password: password_hash.clone(),
                email: None,
                role: "admin".to_string(),
                tenant_id: "default".to_string(),
                created_at: chrono::Utc::now().to_rfc3339(),
                last_login_at: None,
                must_change_password: false,
            })
            .await
            .unwrap();
        password_hash
    }

    /// 铸造内置 admin 的 access token（口令绑定校验要求 token 内嵌哈希与库内
    /// 存储哈希一致——seed 与铸 token 必须共用同一哈希串，不可各自重算）。
    fn admin_token(password_hash: &str) -> String {
        let admin = agentos_http::auth::BuiltInUser {
            id: ADMIN_USER_ID.to_string(),
            username: "admin".to_string(),
            password: password_hash.to_string(),
            email: String::new(),
            role: "admin".to_string(),
            tenant_id: "default".to_string(),
            created_at: String::new(),
            must_change_password: false,
        };
        agentos_http::auth::encode_token(agentos_http::auth::TokenType::Access, &admin, 3600)
    }

    fn bearer(token: &str) -> HeaderMap {
        let mut h = HeaderMap::new();
        h.insert("authorization", format!("Bearer {token}").parse().unwrap());
        h
    }

    /// 可配置行为的存储 mock：各被测方法默认返回正常空值，测试按需注入故障。
    /// 未被任何被测路径触碰的方法以 unreachable! 桩实现（意外耦合即炸出）。
    pub(super) struct FlexMock {
        pub(super) list_sessions_result: Mutex<Result<Vec<SessionRecord>, StorageError>>,
        pub(super) pipeline_ids_by_thread:
            Mutex<HashMap<String, Result<Vec<String>, StorageError>>>,
        pub(super) get_session_result: Mutex<Result<Option<SessionRecord>, StorageError>>,
        pub(super) update_session_result: Mutex<Result<(), StorageError>>,
        /// update_session 实际调用次数（投影随写/零副作用断言用）。
        pub(super) update_session_calls: Mutex<usize>,
        pub(super) create_session_result: Mutex<Result<(), StorageError>>,
        pub(super) upsert_state_field_result: Mutex<Result<(), StorageError>>,
        pub(super) get_messages_result: Mutex<Result<Vec<MessageRecord>, StorageError>>,
    }

    impl Default for FlexMock {
        fn default() -> Self {
            Self {
                list_sessions_result: Mutex::new(Ok(Vec::new())),
                pipeline_ids_by_thread: Mutex::new(HashMap::new()),
                get_session_result: Mutex::new(Ok(None)),
                update_session_result: Mutex::new(Ok(())),
                update_session_calls: Mutex::new(0),
                create_session_result: Mutex::new(Ok(())),
                upsert_state_field_result: Mutex::new(Ok(())),
                get_messages_result: Mutex::new(Ok(Vec::new())),
            }
        }
    }

    impl FlexMock {
        pub(super) fn with_sessions(sessions: Vec<SessionRecord>) -> std::sync::Arc<Self> {
            let mock = Self::default();
            *mock.list_sessions_result.lock().unwrap() = Ok(sessions);
            std::sync::Arc::new(mock)
        }

        pub(super) fn set_pipeline_ids(
            &self,
            thread_id: &str,
            r: Result<Vec<String>, StorageError>,
        ) {
            self.pipeline_ids_by_thread
                .lock()
                .unwrap()
                .insert(thread_id.to_string(), r);
        }
    }

    pub(super) type SErr = StorageError;

    #[async_trait::async_trait]
    impl StorageBackend for FlexMock {
        async fn list_sessions(
            &self,
            _filter: SessionListFilter,
        ) -> Result<Vec<SessionRecord>, SErr> {
            self.list_sessions_result.lock().unwrap().clone()
        }
        async fn list_pipeline_ids_by_thread(
            &self,
            thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<String>, SErr> {
            match self.pipeline_ids_by_thread.lock().unwrap().get(thread_id) {
                Some(r) => r.clone(),
                None => Ok(vec![]),
            }
        }
        async fn get_session(&self, _thread_id: &str) -> Result<Option<SessionRecord>, SErr> {
            self.get_session_result.lock().unwrap().clone()
        }
        async fn update_session(&self, _session: &SessionRecord) -> Result<(), SErr> {
            *self.update_session_calls.lock().unwrap() += 1;
            self.update_session_result.lock().unwrap().clone()
        }
        async fn create_session(&self, _session: &SessionRecord) -> Result<(), SErr> {
            self.create_session_result.lock().unwrap().clone()
        }
        async fn upsert_state_field(
            &self,
            _pipeline_id: &str,
            _tenant_id: &str,
            _key: &str,
            _value: &serde_json::Value,
        ) -> Result<(), SErr> {
            self.upsert_state_field_result.lock().unwrap().clone()
        }
        async fn get_messages_by_pipeline(
            &self,
            _pipeline_id: &str,
            _opts: MessageQueryOpts,
        ) -> Result<Vec<MessageRecord>, SErr> {
            self.get_messages_result.lock().unwrap().clone()
        }
        async fn get_run(&self, _run_id: &str) -> Result<RunRecord, SErr> {
            unreachable!("crud 测试路径不应触碰")
        }
        async fn get_blob(&self, _blob_id: &str) -> Result<Vec<u8>, SErr> {
            unreachable!("crud 测试路径不应触碰")
        }
        async fn append_trace(&self, _entry: TraceEntry) -> Result<(), SErr> {
            unreachable!("crud 测试路径不应触碰")
        }
        async fn store_blob(&self, _data: &[u8], _mime_type: &str) -> Result<String, SErr> {
            unreachable!("crud 测试路径不应触碰")
        }
        async fn delete_session(&self, _thread_id: &str) -> Result<Vec<String>, SErr> {
            unreachable!("crud 测试路径不应触碰")
        }
        async fn link_pipeline_session(
            &self,
            _pipeline_id: &str,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<(), SErr> {
            Ok(())
        }
        async fn get_step_traces_by_thread(
            &self,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<TraceEntry>, SErr> {
            unreachable!("crud 测试路径不应触碰")
        }
        async fn create_user(&self, _user: &UserRecord) -> Result<(), SErr> {
            unreachable!("crud 测试路径不应触碰")
        }
        async fn get_user_by_id(&self, _user_id: &str) -> Result<Option<UserRecord>, SErr> {
            Ok(None)
        }
        async fn get_user_by_username(&self, _username: &str) -> Result<Option<UserRecord>, SErr> {
            Ok(None)
        }
        async fn list_users(&self) -> Result<Vec<UserRecord>, SErr> {
            unreachable!("crud 测试路径不应触碰")
        }
        async fn update_last_login(&self, _user_id: &str) -> Result<(), SErr> {
            unreachable!("crud 测试路径不应触碰")
        }
        async fn update_user_password(
            &self,
            _user_id: &str,
            _password_hash: &str,
            _must_change_password: bool,
        ) -> Result<bool, SErr> {
            unreachable!("mock 不提供口令更新")
        }
        async fn delete_user(&self, _user_id: &str) -> Result<bool, SErr> {
            unreachable!("crud 测试路径不应触碰")
        }
    }

    pub(super) fn state_with(
        store: Option<std::sync::Arc<dyn StorageBackend>>,
        session: bool,
    ) -> AppState {
        let mut state = AppState::new();
        state.store = store;
        if session {
            state.session = Some(std::sync::Arc::new(
                agentos_session::SessionCoordinator::new(),
            ));
        }
        state
    }

    // ── create_session ────────────────────────────────────────────────────

    #[tokio::test]
    async fn create_session_falls_intent_back_to_title_and_merges_metadata_defaults() {
        // 无 token 无 body user_id → anonymous；intent 缺省回落 title；
        // 自定义 metadata 与默认键合并（session_type/user_id 注入，原字段保留）。
        let store = sqlite();
        let state = state_with(
            Some(store.clone() as std::sync::Arc<dyn StorageBackend>),
            true,
        );

        let resp = create_session_handler(
            State(state),
            HeaderMap::new(),
            Json(json!({"title": "会话标题A", "metadata": {"custom": "v"}})),
        )
        .await
        .unwrap();
        let body = resp.0;
        assert_eq!(
            body["intent"],
            json!("会话标题A"),
            "intent 缺省回落 title: {body}"
        );
        assert_eq!(body["title"], json!("会话标题A"));
        assert_eq!(body["agent_id"], json!("agentos"));
        assert_eq!(body["metadata"]["session_type"], json!("main_pipeline"));
        assert_eq!(body["metadata"]["user_id"], json!("anonymous"));
        assert_eq!(
            body["metadata"]["custom"],
            json!("v"),
            "自定义 metadata 键不得被默认值覆盖"
        );
        assert_eq!(body["pipeline_ids"].as_array().unwrap().len(), 1);
        assert_eq!(body["current_state"], json!("active"));

        // 落库真值：metadata 合并结果与 title 持久化（title 非空 → Some）
        let tid = body["thread_id"].as_str().unwrap().to_string();
        let rec = store.get_session(&tid).await.unwrap().unwrap();
        assert_eq!(rec.title.as_deref(), Some("会话标题A"));
        let meta = rec.metadata.unwrap();
        assert_eq!(meta["session_type"], json!("main_pipeline"));
        assert_eq!(meta["user_id"], json!("anonymous"));
    }

    #[tokio::test]
    async fn create_session_body_user_id_wins_only_without_token_user() {
        // A14：token 用户是事件路由权威源；仅无 token 用户时才降级用 body user_id。
        let store = sqlite();
        let password_hash = seed_admin(&store).await;
        let mut state = state_with(
            Some(store.clone() as std::sync::Arc<dyn StorageBackend>),
            true,
        );
        state.db = Some(store.clone());

        // ① token + body 不一致 → token 赢
        let resp = create_session_handler(
            State(state.clone()),
            bearer(&admin_token(&password_hash)),
            Json(json!({"user_id": "attacker", "intent": "t"})),
        )
        .await
        .unwrap();
        assert_eq!(
            resp.0["metadata"]["user_id"],
            json!(ADMIN_USER_ID),
            "token 用户必须压过 body user_id: {}",
            resp.0
        );
        let tid = resp.0["thread_id"].as_str().unwrap().to_string();
        let registry = state.session.as_ref().unwrap().registry();
        assert_eq!(
            registry.get_user_for_thread(&tid).as_deref(),
            Some(ADMIN_USER_ID),
            "thread→user 映射必须按 token 用户登记（WS 事件路由依据）"
        );

        // ② 无 token + body user_id → 显式降级采用 body 值
        let resp = create_session_handler(
            State(state),
            HeaderMap::new(),
            Json(json!({"user_id": "u-body", "intent": "t"})),
        )
        .await
        .unwrap();
        assert_eq!(resp.0["metadata"]["user_id"], json!("u-body"));
    }

    #[tokio::test]
    async fn create_session_empty_agent_id_skips_pipeline_state_binding() {
        // agent_id 空串 = 未指定：响应原样回显，DB 记录 agent_id 为 None，
        // 且不写主管道 state（出生绑定只对显式 agent 生效）。
        let store = sqlite();
        let mut state = state_with(
            Some(store.clone() as std::sync::Arc<dyn StorageBackend>),
            true,
        );
        state.db = Some(store.clone());

        let resp = create_session_handler(
            State(state),
            HeaderMap::new(),
            Json(json!({"intent": "t", "agent_id": ""})),
        )
        .await
        .unwrap();
        assert_eq!(resp.0["agent_id"], json!(""));
        let pid = resp.0["active_pipeline_id"].as_str().unwrap().to_string();

        let tid = resp.0["thread_id"].as_str().unwrap().to_string();
        let rec = store.get_session(&tid).await.unwrap().unwrap();
        assert!(rec.agent_id.is_none(), "空串 agent_id 不得落库为绑定");

        let fields = store.load_pipeline_state(&pid, "default").unwrap();
        assert!(
            !fields.contains_key("agent.id"),
            "未指定 agent 不得写主管道 state 绑定，实际: {fields:?}"
        );
    }

    #[tokio::test]
    async fn create_session_without_store_still_registers_memory_mappings() {
        // store 未配置（无持久化数据源）：跳过落库，内存 registry 三映射照常登记
        // （WS 路由热路径依赖）。
        let state = state_with(None, true);
        let resp = create_session_handler(
            State(state.clone()),
            HeaderMap::new(),
            Json(json!({"intent": "t", "agent_id": "general_agent"})),
        )
        .await
        .unwrap();
        let body = resp.0;
        let tid = body["thread_id"].as_str().unwrap().to_string();
        let pid = body["active_pipeline_id"].as_str().unwrap().to_string();
        let registry = state.session.as_ref().unwrap().registry();
        assert_eq!(
            registry.get_pipeline_for_thread(&tid).as_deref(),
            Some(pid.as_str())
        );
        assert_eq!(
            registry.get_agent_for_thread(&tid).as_deref(),
            Some("general_agent")
        );
    }

    #[tokio::test]
    async fn create_session_db_failure_returns_response_anyway() {
        // 落库失败仅 warn 不阻断响应（registry 已登记，WS 路由不受影响）。
        let mock = std::sync::Arc::new(FlexMock::default());
        *mock.create_session_result.lock().unwrap() = Err(SErr::Database("injected".into()));
        let state = state_with(Some(mock as std::sync::Arc<dyn StorageBackend>), true);

        let resp =
            create_session_handler(State(state), HeaderMap::new(), Json(json!({"intent": "t"})))
                .await
                .unwrap();
        assert!(
            resp.0["thread_id"].as_str().is_some(),
            "落库失败不得吞掉创建响应: {}",
            resp.0
        );
        assert_eq!(resp.0["pipeline_ids"].as_array().unwrap().len(), 1);
    }

    // ── update_session_agent ──────────────────────────────────────────────

    #[tokio::test]
    async fn update_session_agent_without_db_record_returns_404() {
        // store 在而会话不在（本租户无记录）：404 且零副作用——registry 不得
        // 被写入（修复前此处回退内存构造伪造 200 并写 registry）。
        let store = sqlite();
        let state = state_with(Some(store as std::sync::Arc<dyn StorageBackend>), true);
        let registry = state.session.as_ref().unwrap().registry();
        registry.register_thread("T-fb", ADMIN_USER_ID);
        registry.register_thread_pipeline("T-fb", "p-fb");

        let resp = update_session_agent_handler(
            State(state.clone()),
            HeaderMap::new(),
            Path("T-fb".into()),
            Json(json!({"agent_id": "custom-x"})),
        )
        .await;
        match resp {
            Ok(body) => panic!("DB 无记录不得伪造 200 回退响应: {:?}", body.0),
            Err(e) => assert!(
                matches!(e, ApiError::NotFound { .. }),
                "无记录应 404，实际 {e:?}"
            ),
        }
        assert_eq!(
            registry.get_agent_for_thread("T-fb").as_deref(),
            None,
            "404 路径不得写内存 registry"
        );
    }

    #[tokio::test]
    async fn update_session_agent_fails_when_pipeline_state_write_fails() {
        // ③ 真值写失败 = 绑定更新失败（503 可重试）：registry 缓存与 sessions
        // 表投影均不写（缓存/投影不得领先真值），不再容忍静默放行——放行会让
        // 重启后冷恢复读 state 时丢绑定。
        let mock = std::sync::Arc::new(FlexMock::default());
        *mock.get_session_result.lock().unwrap() = Ok(Some(session_record("T1", Some("p1"))));
        *mock.upsert_state_field_result.lock().unwrap() = Err(SErr::Database("injected".into()));
        let state = state_with(
            Some(mock.clone() as std::sync::Arc<dyn StorageBackend>),
            true,
        );
        // thread→主管道映射登记（registry 热命中=真值落点加速路径），确保
        // ③ 真值写被触达、注入的 upsert 故障正好落在真值写上。
        state
            .session
            .as_ref()
            .unwrap()
            .registry()
            .register_thread_pipeline("T1", "p1");
        let registry = state.session.as_ref().unwrap().registry().clone();

        let resp = update_session_agent_handler(
            State(state),
            HeaderMap::new(),
            Path("T1".into()),
            Json(json!({"agent_id": "x-agent"})),
        )
        .await;
        match resp {
            Ok(body) => panic!("state 真值写失败不得伪造 200: {:?}", body.0),
            Err(e) => assert!(
                matches!(e, ApiError::ServiceUnavailable { .. }),
                "真值写失败应 503 可重试，实际 {e:?}"
            ),
        }
        assert_eq!(
            registry.get_agent_for_thread("T1").as_deref(),
            None,
            "失败路径不得写内存 registry"
        );
        assert_eq!(
            *mock.update_session_calls.lock().unwrap(),
            0,
            "失败路径不得写 sessions 表投影"
        );
    }

    #[tokio::test]
    async fn update_session_agent_projects_to_sessions_after_state_truth() {
        // ③ 真值成功后 ② 投影随写：state agent.id 与 sessions.agent_id 同值。
        // 冷恢复只读 state 单源（F2），投影仅供会话列表回显。
        let store = sqlite();
        store
            .create_session(&session_record("T-prj", Some("p-prj")))
            .await
            .unwrap();
        let state = state_with(
            Some(store.clone() as std::sync::Arc<dyn StorageBackend>),
            true,
        );
        state
            .session
            .as_ref()
            .unwrap()
            .registry()
            .register_thread_pipeline("T-prj", "p-prj");

        let resp = update_session_agent_handler(
            State(state),
            HeaderMap::new(),
            Path("T-prj".into()),
            Json(json!({"agent_id": "x-agent"})),
        )
        .await
        .unwrap();
        assert_eq!(resp.0["agent_id"], json!("x-agent"));
        let fields = store.load_pipeline_state("p-prj", "default").unwrap();
        assert_eq!(
            fields.get("agent.id").and_then(|v| v.as_str()),
            Some("x-agent"),
            "绑定真值落主管道 state"
        );
        let rec = store.get_session("T-prj").await.unwrap().unwrap();
        assert_eq!(
            rec.agent_id.as_deref(),
            Some("x-agent"),
            "真值成功后投影随写 sessions 表"
        );
    }

    #[tokio::test]
    async fn update_session_agent_cold_registry_resolves_pipeline_from_persistent_truth() {
        // X5-F1：内核重启后 registry 无 thread→pipeline 映射（WS 重连仅注册
        // thread→user），③ 真值写不得静默跳过——从持久真值（sessions.
        // active_pipeline_id + pipeline_sessions 映射）解析主管道照写。修复前
        // 此处 ③ 被静默跳过、仅 ② 投影更新即返回成功（UI 假成功，冷恢复丢绑定）。
        let store = sqlite();
        store
            .create_session(&session_record("T-cold", Some("p-cold")))
            .await
            .unwrap();
        store
            .link_pipeline_session("p-cold", "T-cold", "default")
            .await
            .unwrap();
        let state = state_with(
            Some(store.clone() as std::sync::Arc<dyn StorageBackend>),
            true,
        );
        // 关键：不注册 thread→pipeline（模拟内核重启后的冷 registry）。

        let resp = update_session_agent_handler(
            State(state.clone()),
            HeaderMap::new(),
            Path("T-cold".into()),
            Json(json!({"agent_id": "cold-agent"})),
        )
        .await
        .unwrap();
        assert_eq!(resp.0["agent_id"], json!("cold-agent"));
        // ③ 绑定真值已落主管道 state（冷恢复单源读面 resolve_dispatch_agent
        // 即从该管道读 agent.id）
        let fields = store.load_pipeline_state("p-cold", "default").unwrap();
        assert_eq!(
            fields.get("agent.id").and_then(|v| v.as_str()),
            Some("cold-agent"),
            "冷 registry 下绑定真值仍必须落主管道 state"
        );
        // ② 投影随写 + ① registry 热绑定照常
        let rec = store.get_session("T-cold").await.unwrap().unwrap();
        assert_eq!(rec.agent_id.as_deref(), Some("cold-agent"), "投影随写");
        let registry = state.session.as_ref().unwrap().registry();
        assert_eq!(
            registry.get_agent_for_thread("T-cold").as_deref(),
            Some("cold-agent"),
            "registry 热绑定同步"
        );
    }

    #[tokio::test]
    async fn update_session_agent_fails_closed_when_no_persistent_pipeline() {
        // 持久真值也查不到 → 显式 503，② sessions 投影与 ① registry 均不写
        // （绝不只更新投影就返回成功——那会重开 ADR 2026-10-08 的漂移通道）。
        // 两组有区分度输入：遗留行 active_pipeline_id 缺失；active 指针在但
        // pipeline_sessions 映射缺失（数据异常，真值不得写进无归属管道）。
        let store = sqlite();
        store
            .create_session(&session_record("T-legacy", None))
            .await
            .unwrap();
        store
            .create_session(&session_record("T-orphan", Some("p-orphan")))
            .await
            .unwrap();
        let state = state_with(
            Some(store.clone() as std::sync::Arc<dyn StorageBackend>),
            true,
        );

        // 输入①：无主管道指针（遗留行）
        let resp = update_session_agent_handler(
            State(state.clone()),
            HeaderMap::new(),
            Path("T-legacy".into()),
            Json(json!({"agent_id": "x-agent"})),
        )
        .await;
        match resp {
            Ok(body) => panic!("无持久主管道不得伪造成功: {:?}", body.0),
            Err(e) => assert!(
                matches!(e, ApiError::ServiceUnavailable { .. }),
                "持久真值缺失应 503 可重试，实际 {e:?}"
            ),
        }
        let rec = store.get_session("T-legacy").await.unwrap().unwrap();
        assert_eq!(rec.agent_id, None, "失败路径不得写 sessions 投影");
        let registry = state.session.as_ref().unwrap().registry();
        assert_eq!(
            registry.get_agent_for_thread("T-legacy").as_deref(),
            None,
            "失败路径不得写内存 registry"
        );

        // 输入②：active 指针在、映射行缺失
        let resp = update_session_agent_handler(
            State(state.clone()),
            HeaderMap::new(),
            Path("T-orphan".into()),
            Json(json!({"agent_id": "y-agent"})),
        )
        .await;
        match resp {
            Ok(body) => panic!("映射缺失不得伪造成功: {:?}", body.0),
            Err(e) => assert!(
                matches!(e, ApiError::ServiceUnavailable { .. }),
                "映射缺失应 503 可重试，实际 {e:?}"
            ),
        }
        let rec = store.get_session("T-orphan").await.unwrap().unwrap();
        assert_ne!(
            rec.agent_id.as_deref(),
            Some("y-agent"),
            "映射缺失路径不得写 sessions 投影"
        );
        assert_eq!(
            state
                .session
                .as_ref()
                .unwrap()
                .registry()
                .get_agent_for_thread("T-orphan")
                .as_deref(),
            None,
            "映射缺失路径不得写内存 registry"
        );
    }

    #[tokio::test]
    async fn create_session_fails_when_birth_agent_state_write_fails() {
        // 出生绑定真值写失败 = 创建失败（503 可重试）：会话行已持久化而绑定
        // 真值缺失会让重启后冷恢复丢绑定（F2 漂移通道），不再静默放行。
        // 对照：create_session 本身失败仍 warn 继续返回（无持久化即无真值可丢，
        // 见 create_session_db_failure_returns_response_anyway）。
        let mock = std::sync::Arc::new(FlexMock::default());
        *mock.upsert_state_field_result.lock().unwrap() = Err(SErr::Database("injected".into()));
        let state = state_with(Some(mock as std::sync::Arc<dyn StorageBackend>), true);

        let resp = create_session_handler(
            State(state),
            HeaderMap::new(),
            Json(json!({"intent": "t", "agent_id": "custom-a"})),
        )
        .await;
        match resp {
            Ok(body) => panic!("出生绑定真值写失败不得伪造 200: {:?}", body.0),
            Err(e) => assert!(
                matches!(e, ApiError::ServiceUnavailable { .. }),
                "出生绑定写失败应 503 可重试，实际 {e:?}"
            ),
        }
    }

    #[tokio::test]
    async fn update_session_agent_tolerates_db_update_failure() {
        // update_session 落库失败仅 warn：响应仍按内存更新值构造。
        // 前置：真值落点就绪（registry 热映射=加速路径），本测聚焦 ② 投影容忍。
        let mock = std::sync::Arc::new(FlexMock::default());
        *mock.get_session_result.lock().unwrap() = Ok(Some(session_record("T1", Some("p1"))));
        *mock.update_session_result.lock().unwrap() = Err(SErr::Database("injected".into()));
        let state = state_with(
            Some(mock.clone() as std::sync::Arc<dyn StorageBackend>),
            true,
        );
        state
            .session
            .as_ref()
            .unwrap()
            .registry()
            .register_thread_pipeline("T1", "p1");

        let resp = update_session_agent_handler(
            State(state),
            HeaderMap::new(),
            Path("T1".into()),
            Json(json!({"agent_id": "y-agent"})),
        )
        .await
        .unwrap();
        assert_eq!(
            resp.0["agent_id"],
            json!("y-agent"),
            "落库失败不得吞更新响应"
        );
    }

    // ── update_session（重命名/更新）───────────────────────────────────────

    #[tokio::test]
    async fn update_session_persists_title_intent_when_record_exists() {
        let store = sqlite();
        store
            .create_session(&session_record("T-r", Some("p-r")))
            .await
            .unwrap();
        let state = state_with(
            Some(store.clone() as std::sync::Arc<dyn StorageBackend>),
            false,
        );

        // ① 带 intent：title 与 intent 同时更新
        let resp = update_session_handler(
            State(state.clone()),
            HeaderMap::new(),
            Path("T-r".into()),
            Json(json!({"intent": "新名"})),
        )
        .await
        .unwrap();
        assert_eq!(resp.0["title"], json!("新名"));
        assert_eq!(resp.0["intent"], json!("新名"));
        let rec = store.get_session("T-r").await.unwrap().unwrap();
        assert_eq!(rec.title.as_deref(), Some("新名"));
        assert_eq!(rec.intent.as_deref(), Some("新名"));

        // ② 不带 intent：仅更新 updated_at，title 不动
        let before = rec.updated_at.clone();
        let resp = update_session_handler(
            State(state),
            HeaderMap::new(),
            Path("T-r".into()),
            Json(json!({})),
        )
        .await
        .unwrap();
        assert_eq!(resp.0["title"], json!("新名"), "缺 intent 不得清空既有标题");
        let rec = store.get_session("T-r").await.unwrap().unwrap();
        assert_ne!(rec.updated_at, before, "touch 语义：updated_at 刷新");
    }

    #[tokio::test]
    async fn pipeline_title_written_on_create_and_rename_propagation() {
        // 统一管道标题（ADR 2026-10-27-pipeline-title-field）：①创建带标题 →
        // 主管道 state.title 出生写入；②改名（intent 承载标题）→ 传播刷新。
        // 无回退腿：无 title 键 = runs 出口无该字段。
        let store = sqlite();
        let state = state_with(
            Some(store.clone() as std::sync::Arc<dyn StorageBackend>),
            true,
        );

        let resp = create_session_handler(
            State(state.clone()),
            HeaderMap::new(),
            Json(json!({"title": "出生标题"})),
        )
        .await
        .unwrap();
        let thread_id = resp.0["thread_id"].as_str().unwrap().to_string();
        let pid = resp.0["pipeline_ids"][0].as_str().unwrap().to_string();
        let fields = store.load_pipeline_state(&pid, "default").unwrap();
        assert_eq!(
            fields.get("title"),
            Some(&json!("出生标题")),
            "创建带标题 → 主管道 title 出生落键"
        );

        // 改名：标题经 update_session（intent 承载）传播刷新主管道 title
        let resp = update_session_handler(
            State(state),
            HeaderMap::new(),
            Path(thread_id),
            Json(json!({"intent": "改名后"})),
        )
        .await
        .unwrap();
        assert_eq!(resp.0["title"], json!("改名后"));
        let fields = store.load_pipeline_state(&pid, "default").unwrap();
        assert_eq!(
            fields.get("title"),
            Some(&json!("改名后")),
            "标题变更传播到主管道 state.title"
        );
    }

    #[tokio::test]
    async fn update_session_merges_metadata_and_preserves_existing_keys() {
        // 前端置顶/星标走 PATCH metadata（sessionListStore toggleSessionPin/
        // toggleSessionStar）：body.metadata 必须按 key 合并进既有 metadata，
        // 不得整体替换（会抹掉 session_type/user_id，列表过滤与租户归属失真），
        // 更不得静默丢弃（丢弃则置顶态只活在前端缓存，列表一刷新即回退）。
        let store = sqlite();
        let mut seeded = session_record("T-md", Some("p-md"));
        seeded.metadata = Some(json!({
            "session_type": "main_pipeline",
            "user_id": ADMIN_USER_ID,
        }));
        store.create_session(&seeded).await.unwrap();
        let state = state_with(
            Some(store.clone() as std::sync::Arc<dyn StorageBackend>),
            false,
        );

        // ① PATCH metadata {pinned:true}：落库 + 响应携带，既有 key 保全
        let resp = update_session_handler(
            State(state.clone()),
            HeaderMap::new(),
            Path("T-md".into()),
            Json(json!({"metadata": {"pinned": true}})),
        )
        .await
        .unwrap();
        assert_eq!(
            resp.0["metadata"]["pinned"],
            json!(true),
            "响应携带合并后 metadata"
        );
        assert_eq!(
            resp.0["metadata"]["session_type"],
            json!("main_pipeline"),
            "既有 session_type 不得被抹掉"
        );
        let rec = store.get_session("T-md").await.unwrap().unwrap();
        let meta = rec.metadata.as_ref().unwrap();
        assert_eq!(meta["pinned"], json!(true), "metadata.pinned 必须落库");
        assert_eq!(meta["session_type"], json!("main_pipeline"));
        assert_eq!(meta["user_id"], json!(ADMIN_USER_ID));
        assert_ne!(
            rec.updated_at, seeded.updated_at,
            "touch 语义：updated_at 刷新"
        );

        // ② 再 PATCH {pinned:false}：同一 key 覆盖为 false（取消置顶可落库）
        let resp = update_session_handler(
            State(state.clone()),
            HeaderMap::new(),
            Path("T-md".into()),
            Json(json!({"metadata": {"pinned": false}})),
        )
        .await
        .unwrap();
        assert_eq!(resp.0["metadata"]["pinned"], json!(false));
        let rec = store.get_session("T-md").await.unwrap().unwrap();
        assert_eq!(rec.metadata.as_ref().unwrap()["pinned"], json!(false));

        // ③ 不带 metadata：既有 metadata 原样保留（纯改名不清元数据）
        let resp = update_session_handler(
            State(state),
            HeaderMap::new(),
            Path("T-md".into()),
            Json(json!({"intent": "新名"})),
        )
        .await
        .unwrap();
        assert_eq!(resp.0["title"], json!("新名"));
        let rec = store.get_session("T-md").await.unwrap().unwrap();
        let meta = rec.metadata.as_ref().unwrap();
        assert_eq!(meta["pinned"], json!(false));
        assert_eq!(meta["session_type"], json!("main_pipeline"));
    }

    #[tokio::test]
    async fn update_session_without_db_record_returns_404() {
        // store 在而会话不在（本租户无记录）：404，不再回退内存构造伪造 200
        // （非 default 租户用户改自己会话此前正是命中该伪造路径）。
        let store = sqlite();
        let state = state_with(Some(store as std::sync::Arc<dyn StorageBackend>), true);
        let registry = state.session.as_ref().unwrap().registry();
        registry.register_thread("T-fb2", ADMIN_USER_ID);
        registry.register_thread_pipeline("T-fb2", "p-fb2");
        registry.register_thread_agent("T-fb2", "a-fb2");

        let resp = update_session_handler(
            State(state),
            HeaderMap::new(),
            Path("T-fb2".into()),
            Json(json!({"intent": "改名"})),
        )
        .await;
        match resp {
            Ok(body) => panic!("DB 无记录不得伪造 200 回退响应: {:?}", body.0),
            Err(e) => assert!(
                matches!(e, ApiError::NotFound { .. }),
                "无记录应 404，实际 {e:?}"
            ),
        }
    }

    #[tokio::test]
    async fn update_handlers_without_coordinator_return_minimal_fallback() {
        // store 与 session 均未配置（纯内存兼容模式）：两个 PATCH 均走最小回退，
        // agent/pipeline 字段为空。
        let state = state_with(None, false);

        let resp = update_session_handler(
            State(state.clone()),
            HeaderMap::new(),
            Path("T".into()),
            Json(json!({"intent": "x"})),
        )
        .await
        .unwrap();
        assert_eq!(resp.0["intent"], json!("x"));
        assert_eq!(resp.0["agent_id"], Value::Null);
        assert_eq!(resp.0["pipeline_ids"], json!([]));

        let resp = update_session_agent_handler(
            State(state),
            HeaderMap::new(),
            Path("T".into()),
            Json(json!({"agent_id": "solo"})),
        )
        .await
        .unwrap();
        assert_eq!(resp.0["agent_id"], json!("solo"));
        assert_eq!(resp.0["pipeline_ids"], json!([]));
    }

    // ── list_session_messages ─────────────────────────────────────────────

    #[tokio::test]
    async fn list_messages_without_store_returns_empty_page() {
        let state = state_with(None, false);
        let resp = list_session_messages_handler(
            State(state),
            HeaderMap::new(),
            Path("T".into()),
            Query(MessageListQuery {
                pipeline_run_id: None,
                before_sequence: None,
                after_sequence: None,
                limit: None,
            }),
        )
        .await
        .unwrap();
        assert_eq!(resp.0["messages"], json!([]));
        assert_eq!(resp.0["total"], json!(0));
        assert_eq!(resp.0["has_more"], json!(false));
    }

    fn seed_messages(
        store: &std::sync::Arc<agentos_engine::SqliteStore>,
        pipeline: &str,
        contents: &[&str],
    ) {
        let ops: Vec<serde_json::Value> = contents
            .iter()
            .enumerate()
            .map(|(i, c)| json!({"op": "set", "seq": i, "msg": {"role": "user", "content": c}}))
            .collect();
        store
            .apply_messages_ops_to_table(pipeline, "default", &ops)
            .unwrap();
    }

    #[tokio::test]
    async fn list_messages_resolves_active_pipeline_by_thread_without_explicit_pid() {
        // GAP-1：未传 pipeline_run_id 时按会话 active_pipeline_id 查询；
        // active 缺失/空串 → 回退路径 id（thread_id）。
        let store = sqlite();
        store
            .create_session(&session_record("T-a", Some("pa")))
            .await
            .unwrap();
        store
            .link_pipeline_session("pa", "T-a", "default")
            .await
            .unwrap();
        seed_messages(&store, "pa", &["active管道消息"]);
        // active None / 空串两个对照会话（目标管道无消息 → 空）
        store
            .create_session(&session_record("T-b", None))
            .await
            .unwrap();
        let mut empty_active = session_record("T-c", Some(""));
        empty_active.pipeline_ids = vec![];
        store.create_session(&empty_active).await.unwrap();

        let state = state_with(
            Some(store.clone() as std::sync::Arc<dyn StorageBackend>),
            false,
        );
        let query = |pid: Option<String>| MessageListQuery {
            pipeline_run_id: pid,
            before_sequence: None,
            after_sequence: None,
            limit: None,
        };

        // ① active 管道被采用（thread_id 直查会落空——引擎落库用 resolve 后的管道）
        let resp = list_session_messages_handler(
            State(state.clone()),
            HeaderMap::new(),
            Path("T-a".into()),
            Query(query(None)),
        )
        .await
        .unwrap();
        let msgs = resp.0["messages"].as_array().unwrap();
        assert_eq!(msgs.len(), 1, "应按 active_pipeline_id=pa 命中: {}", resp.0);
        assert_eq!(msgs[0]["content"], json!("active管道消息"));
        assert_eq!(
            msgs[0]["thread_id"],
            json!("T-a"),
            "回填路径 id 供前端 mapper"
        );

        // ② / ③ active None 与空串：回退路径 id，无消息
        for tid in ["T-b", "T-c"] {
            let resp = list_session_messages_handler(
                State(state.clone()),
                HeaderMap::new(),
                Path(tid.to_string()),
                Query(query(None)),
            )
            .await
            .unwrap();
            assert_eq!(resp.0["total"], json!(0), "tid={tid} 无 active 管道应查空");
        }
    }

    #[tokio::test]
    async fn list_messages_restores_interrupted_status_from_blob() {
        // 缺陷B（台账 R1）：中断半截消息 DB blob status=interrupted 落库正确、
        // WS new_message 亦带 interrupted；REST 冷路径此前读 slot 行 status 列
        // （assistant 恒 NULL → 回退 completed）——刷新后「已中断」标识丢失，
        // 冷热读面矛盾。修复后 REST 读面与 WS 热路径（message_status_from_blob）
        // 同源：blob status 透出，冷热同构。
        let store = sqlite();
        let ops = vec![
            json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "写首诗"}}),
            json!({"op": "set", "seq": 1, "msg": {"role": "assistant", "content": "半截内容", "status": "interrupted"}}),
            json!({"op": "set", "seq": 2, "msg": {"role": "assistant", "content": "完整回复"}}),
        ];
        store
            .apply_messages_ops_to_table("p-int", "default", &ops)
            .unwrap();

        let state = state_with(
            Some(store.clone() as std::sync::Arc<dyn StorageBackend>),
            false,
        );
        let resp = list_session_messages_handler(
            State(state),
            HeaderMap::new(),
            Path("p-int".into()),
            Query(MessageListQuery {
                pipeline_run_id: None,
                before_sequence: None,
                after_sequence: None,
                limit: None,
            }),
        )
        .await
        .unwrap();
        let msgs = resp.0["messages"].as_array().unwrap();
        assert_eq!(msgs.len(), 3);
        assert_eq!(
            msgs[1]["status"],
            json!("interrupted"),
            "中断半截消息冷路径还原 interrupted（与 WS 热路径同构）"
        );
        assert_eq!(
            msgs[2]["status"],
            json!("completed"),
            "正常消息（blob 无 status 键）维持 completed 缺省"
        );
        assert_eq!(msgs[0]["status"], json!("completed"));
    }

    #[tokio::test]
    async fn list_messages_has_more_follows_limit_boundary() {
        let store = sqlite();
        seed_messages(&store, "ph", &["m1", "m2", "m3"]);
        let state = state_with(Some(store as std::sync::Arc<dyn StorageBackend>), false);
        let query = |limit: Option<usize>| MessageListQuery {
            pipeline_run_id: Some("ph".into()),
            before_sequence: None,
            after_sequence: None,
            limit,
        };

        for (limit, expect_total, expect_more) in
            [(Some(2), 2, true), (Some(5), 3, false), (None, 3, false)]
        {
            let resp = list_session_messages_handler(
                State(state.clone()),
                HeaderMap::new(),
                Path("ph".into()),
                Query(query(limit)),
            )
            .await
            .unwrap();
            assert_eq!(resp.0["total"], json!(expect_total), "limit={limit:?}");
            assert_eq!(resp.0["has_more"], json!(expect_more), "limit={limit:?}");
        }
    }

    #[tokio::test]
    async fn list_messages_query_failure_returns_503() {
        let mock = std::sync::Arc::new(FlexMock::default());
        *mock.get_messages_result.lock().unwrap() = Err(SErr::Database("injected".into()));
        let state = state_with(Some(mock as std::sync::Arc<dyn StorageBackend>), false);

        let resp = list_session_messages_handler(
            State(state),
            HeaderMap::new(),
            Path("T".into()),
            Query(MessageListQuery {
                pipeline_run_id: Some("p-fail".into()),
                before_sequence: None,
                after_sequence: None,
                limit: None,
            }),
        )
        .await;
        match resp {
            Ok(body) => panic!("查询故障不得伪装空历史: {:?}", body.0),
            Err(e) => assert!(matches!(e, ApiError::ServiceUnavailable { .. })),
        }
    }

    // ── list_sessions 读面补全 ────────────────────────────────────────────

    #[tokio::test]
    async fn list_sessions_merges_child_pipeline_ids_and_tolerates_lookup_failure() {
        // 读面补全（ADR 2026-08-21）：映射表 id 并入 pipeline_ids（保序去重）；
        // 单个会话的映射查询失败仅 warn，不影响其余会话出口。
        let mut rec_a = session_record("TA", Some("p1"));
        rec_a.pipeline_ids = vec!["p1".into()];
        let mut rec_b = session_record("TB", Some("p1"));
        rec_b.pipeline_ids = vec!["p1".into()];
        let mock = FlexMock::with_sessions(vec![rec_a, rec_b]);
        mock.set_pipeline_ids("TA", Ok(vec!["p2".into(), "p1".into()]));
        mock.set_pipeline_ids("TB", Err(SErr::Database("injected".into())));
        let state = state_with(Some(mock as std::sync::Arc<dyn StorageBackend>), false);

        let resp = list_sessions_handler(State(state), HeaderMap::new())
            .await
            .unwrap();
        let threads = resp.0["threads"].as_array().unwrap();
        assert_eq!(resp.0["total"], json!(2));
        assert_eq!(
            threads[0]["pipeline_ids"],
            json!(["p1", "p2"]),
            "子管道并入且保序去重（主管道在前）"
        );
        assert_eq!(
            threads[1]["pipeline_ids"],
            json!(["p1"]),
            "映射查询失败时会话仍以既有 pipeline_ids 出口"
        );
    }
}

#[cfg(test)]
mod message_shaping_and_fallback_tests {
    //! 消息历史响应塑形分支（工具调用/工具结果信封/思考内容/metadata 回显）
    //! 与 list_sessions 内存 registry 回退、create_session token 仲裁分支补测。

    use std::sync::Arc;

    use agentos_core::traits::StorageBackend;

    use super::*;

    fn sqlite() -> Arc<agentos_engine::SqliteStore> {
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap())
    }

    fn query(pid: Option<String>) -> Query<MessageListQuery> {
        Query(MessageListQuery {
            pipeline_run_id: pid,
            before_sequence: None,
            after_sequence: None,
            limit: None,
        })
    }

    fn state_with_store(store: Arc<agentos_engine::SqliteStore>) -> AppState {
        let mut state = AppState::new();
        state.store = Some(store as Arc<dyn StorageBackend>);
        state
    }

    #[tokio::test]
    async fn list_messages_shapes_tool_envelope_and_aux_fields() {
        let store = sqlite();
        // 三种塑形源：assistant 带工具调用与思考内容；tool 带失败结果信封；
        // user 带 metadata.client_message_id（幂等对账键）。
        let ops = vec![
            json!({"op": "set", "seq": 0, "msg": {
                "role": "assistant",
                "content": "我需要查一下",
                "reasoning_content": "思考中…",
                "tool_calls": [
                    {"id": "call_1", "type": "function",
                     "function": {"name": "search", "arguments": "{}"}}
                ],
            }}),
            json!({"op": "set", "seq": 1, "msg": {
                "role": "tool",
                "content": "搜索失败",
                "tool_call_id": "call_1",
                "tool_result": {
                    "success": false,
                    "error": "timeout",
                    "data": {"diff": 1},
                    "duration_ms": 123.0,
                    "tool_name": "search",
                    "metadata": {"container_task_id": "ct_9"},
                },
            }}),
            json!({"op": "set", "seq": 2, "msg": {
                "role": "user",
                "content": "hi",
                "metadata": {"client_message_id": "cmid_1"},
            }}),
        ];
        store
            .apply_messages_ops_to_table("pipe_shape", "default", &ops)
            .unwrap();
        let state = state_with_store(store);

        let resp = list_session_messages_handler(
            State(state),
            HeaderMap::new(),
            Path("t_any".to_string()),
            query(Some("pipe_shape".to_string())),
        )
        .await
        .unwrap();
        let msgs = resp.0["messages"].as_array().unwrap();
        assert_eq!(msgs.len(), 3);

        // assistant：toolCalls 反序列化为数组 + reasoningContent camelCase
        let assistant = &msgs[0];
        assert_eq!(assistant["role"], "assistant");
        let tool_calls = assistant["toolCalls"].as_array().expect("toolCalls 数组");
        assert_eq!(tool_calls[0]["id"], "call_1");
        assert_eq!(assistant["reasoningContent"], "思考中…");
        assert_eq!(
            assistant["status"], "completed",
            "非 tool 消息回退 completed"
        );

        // tool：失败态还原 + 结构化信封字段（camelCase 对齐前端 mapper）
        let tool = &msgs[1];
        assert_eq!(tool["toolCallId"], "call_1");
        assert_eq!(tool["status"], "failed", "信封 success:false → failed");
        assert_eq!(tool["error"], "timeout");
        assert_eq!(tool["toolResultData"]["diff"], json!(1));
        assert_eq!(tool["toolDurationMs"], json!(123.0));
        assert_eq!(tool["toolName"], "search");
        assert_eq!(tool["containerTaskId"], "ct_9");

        // user：metadata 原样回显（前端乐观消息对账去重键）
        assert_eq!(msgs[2]["metadata"]["client_message_id"], "cmid_1");
    }

    #[tokio::test]
    async fn list_messages_tool_success_envelope_has_completed_without_error() {
        // 第二组输入：信封 success:true → completed 且不带 error 键；
        // 无信封的 tool 消息（裸文本非 Error: 前缀）→ completed。
        let store = sqlite();
        let ops = vec![
            json!({"op": "set", "seq": 0, "msg": {
                "role": "tool",
                "content": "结果正文",
                "tool_call_id": "call_2",
                "tool_result": {"success": true, "data": {"rows": 3}},
            }}),
            json!({"op": "set", "seq": 1, "msg": {
                "role": "tool",
                "content": "旧式纯文本结果",
            }}),
        ];
        store
            .apply_messages_ops_to_table("pipe_shape_ok", "default", &ops)
            .unwrap();
        let state = state_with_store(store);

        let resp = list_session_messages_handler(
            State(state),
            HeaderMap::new(),
            Path("t".to_string()),
            query(Some("pipe_shape_ok".to_string())),
        )
        .await
        .unwrap();
        let msgs = resp.0["messages"].as_array().unwrap();
        assert_eq!(msgs[0]["status"], "completed");
        assert!(msgs[0].get("error").is_none(), "成功结果不得伪造 error");
        assert_eq!(msgs[0]["toolResultData"]["rows"], json!(3));
        assert!(
            msgs[0].get("toolName").is_none(),
            "信封未声明 tool_name 时不附带该键"
        );
        assert_eq!(msgs[1]["status"], "completed", "无信封裸文本回退 completed");
    }

    #[tokio::test]
    async fn list_sessions_without_store_falls_back_to_memory_registry() {
        // store 未配置（兼容场景）+ session 协调器在 → registry 回退：
        // thread 出口 title=null、metadata 带 user_id、pipeline 映射随注册态。
        let mut state = AppState::new();
        let session = Arc::new(agentos_session::SessionCoordinator::new());
        session.registry().register_thread("thread-mem-fb", "u_fb");
        session
            .registry()
            .register_thread_pipeline("thread-mem-fb", "pipe_fb_1");
        state.session = Some(session);

        let resp = list_sessions_handler(State(state), HeaderMap::new())
            .await
            .unwrap();
        let threads = resp.0["threads"].as_array().unwrap();
        assert_eq!(resp.0["total"], json!(1));
        assert_eq!(threads[0]["thread_id"], "thread-mem-fb");
        assert_eq!(threads[0]["title"], Value::Null, "registry 回退无标题");
        assert_eq!(threads[0]["current_state"], "active");
        assert_eq!(threads[0]["metadata"]["user_id"], "u_fb");
        assert_eq!(threads[0]["pipeline_ids"], json!(["pipe_fb_1"]));
        assert_eq!(threads[0]["active_pipeline_id"], "pipe_fb_1");
    }

    #[tokio::test]
    async fn create_session_token_user_wins_over_conflicting_body_user() {
        // token 用户在场且与 body user_id 冲突 → 以 token 用户为事件路由真值
        // （A14：body user_id 可伪造）。store 未接线走内置 admin 回退。
        let state = AppState::new();
        let mut headers = HeaderMap::new();
        let token = agentos_http::auth::encode_token(
            agentos_http::auth::TokenType::Access,
            &agentos_http::auth::BuiltInUser {
                id: "00000000-0000-0000-0000-000000000001".to_string(),
                username: "admin".to_string(),
                password: String::new(),
                email: String::new(),
                role: "admin".to_string(),
                tenant_id: "default".to_string(),
                created_at: String::new(),
                must_change_password: false,
            },
            3600,
        );
        headers.insert("authorization", format!("Bearer {token}").parse().unwrap());

        let resp = create_session_handler(
            State(state),
            headers,
            Json(json!({"user_id": "spoofed_user", "title": "冲突仲裁"})),
        )
        .await
        .unwrap();
        assert_eq!(
            resp.0["metadata"]["user_id"], "00000000-0000-0000-0000-000000000001",
            "事件路由用户必须取 token 用户 id 而非 body 伪造值"
        );
        assert_eq!(resp.0["title"], "冲突仲裁");
        // 主管道短 id 形状：12 位 hex（与任务管道同格式）
        let pid = resp.0["active_pipeline_id"].as_str().unwrap();
        assert_eq!(pid.len(), 12);
        assert!(pid.chars().all(|c| c.is_ascii_hexdigit()));
        assert_eq!(resp.0["pipeline_ids"], json!([pid]));
    }
}

#[cfg(test)]
mod session_routes_logging_tests {
    //! 诊断留痕取证：tracing 宏体只在存在订阅者时求值。行为面已由既有用例覆盖
    //! （503 翻译、降级回退、仲裁取 token），本模块挂采集订阅重跑同路径取证。
    use agentos_core::traits::StorageBackend;

    use super::sessions_crud_tests::{session_record, state_with, FlexMock, SErr, SEED_ADMIN_PW};
    use super::*;

    // ── 诊断留痕批：tracing 宏体只在有订阅者时求值 ─────────────────────────
    //
    // 这些分支的行为断言已由既有用例覆盖（503 翻译、降级回退、仲裁取 token），
    // 但消息行本身恒零命中——本模块挂采集订阅重跑同路径，为留痕取证。

    /// 会话列表：单个会话的映射查询失败 → warn 留痕且其余会话照常出口。
    #[tokio::test]
    async fn list_sessions_pipeline_lookup_failure_is_logged() {
        let (guard, logs) = crate::test_env::capture_logs();
        let _ = &guard;
        let mut rec = session_record("TD", Some("p1"));
        rec.pipeline_ids = vec!["p1".into()];
        let mock = FlexMock::with_sessions(vec![rec]);
        mock.set_pipeline_ids("TD", Err(SErr::Database("injected lookup fault".into())));
        let state = state_with(Some(mock as std::sync::Arc<dyn StorageBackend>), false);

        let resp = list_sessions_handler(State(state), HeaderMap::new())
            .await
            .unwrap();
        let text = logs.text();
        assert_eq!(resp.0["total"], json!(1), "单会话映射失败不得阻断列表");
        assert!(
            text.contains("读映射表失败，pipeline_ids 不完整"),
            "映射表读失败须留痕: {text}"
        );
    }

    /// list_sessions 整体查询失败 → 503 且 warn 留痕（不回退内存假成功）。
    #[tokio::test]
    async fn list_sessions_query_failure_is_logged_and_503() {
        let (guard, logs) = crate::test_env::capture_logs();
        let _ = &guard;
        let mock = std::sync::Arc::new(FlexMock::default());
        *mock.list_sessions_result.lock().unwrap() = Err(SErr::Database("injected".into()));
        let state = state_with(Some(mock as std::sync::Arc<dyn StorageBackend>), true);

        let err = list_sessions_handler(State(state), HeaderMap::new())
            .await
            .expect_err("存储故障必须 503，不得伪装空列表");
        let text = logs.text();
        assert!(matches!(err, ApiError::ServiceUnavailable { .. }));
        assert!(
            text.contains("list_sessions 查询失败，返回 503"),
            "查询失败须 warn 留痕: {text}"
        );
    }

    /// 消息历史查询失败 → 503 且 warn 留痕（不伪装空历史）。
    #[tokio::test]
    async fn list_messages_query_failure_is_logged() {
        let (guard, logs) = crate::test_env::capture_logs();
        let _ = &guard;
        let mock = std::sync::Arc::new(FlexMock::default());
        *mock.get_messages_result.lock().unwrap() = Err(SErr::Database("injected".into()));
        let state = state_with(Some(mock as std::sync::Arc<dyn StorageBackend>), false);

        let err = list_session_messages_handler(
            State(state),
            HeaderMap::new(),
            Path("T-log".into()),
            Query(MessageListQuery {
                pipeline_run_id: Some("p-fail-log".into()),
                before_sequence: None,
                after_sequence: None,
                limit: None,
            }),
        )
        .await
        .expect_err("查询故障不得伪装空历史");
        let text = logs.text();
        assert!(matches!(err, ApiError::ServiceUnavailable { .. }));
        assert!(
            text.contains("get_messages_by_pipeline 查询失败，返回 503"),
            "消息查询失败须 warn 留痕: {text}"
        );
    }

    /// create_session token 仲裁两分支的留痕：body 与 token 不一致（取 token）；
    /// 无 token 有 body（降级取 body，标记可伪造）。
    #[tokio::test]
    async fn create_session_user_arbitration_is_logged() {
        let store = std::sync::Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        store
            .create_user(&UserRecord {
                user_id: "u-arb".to_string(),
                username: "arb_user".to_string(),
                password: agentos_http::auth::hash_password(SEED_ADMIN_PW).unwrap(),
                email: None,
                role: "user".to_string(),
                tenant_id: "u-arb".to_string(),
                created_at: chrono::Utc::now().to_rfc3339(),
                last_login_at: None,
                must_change_password: false,
            })
            .await
            .unwrap();
        let state = state_with(
            Some(store.clone() as std::sync::Arc<dyn StorageBackend>),
            true,
        );
        let token = agentos_http::auth::encode_token(
            agentos_http::auth::TokenType::Access,
            &agentos_http::auth::BuiltInUser::from(
                &store
                    .get_user_by_username("arb_user")
                    .await
                    .unwrap()
                    .unwrap(),
            ),
            3600,
        );

        // 分支一：token 用户 vs body 用户不一致
        let (guard, logs) = crate::test_env::capture_logs();
        let _ = &guard;
        let mut headers = HeaderMap::new();
        headers.insert("authorization", format!("Bearer {token}").parse().unwrap());
        let resp = create_session_handler(
            State(state.clone()),
            headers,
            Json(json!({"title": "仲裁", "user_id": "spoofed"})),
        )
        .await
        .unwrap();
        let text = logs.text();
        assert_eq!(resp.0["metadata"]["user_id"], json!("u-arb"));
        assert!(
            text.contains("body user_id 与 token 用户不一致"),
            "仲裁分支须留痕: {text}"
        );

        // 分支二：无 token 有 body → 降级取 body
        let (guard2, logs2) = crate::test_env::capture_logs();
        let _ = &guard2;
        let resp2 = create_session_handler(
            State(state),
            HeaderMap::new(),
            Json(json!({"title": "降级", "user_id": "embedded_user"})),
        )
        .await
        .unwrap();
        let text2 = logs2.text();
        assert_eq!(resp2.0["metadata"]["user_id"], json!("embedded_user"));
        assert!(
            text2.contains("降级使用 body user_id"),
            "降级分支须留痕（标记可伪造）: {text2}"
        );
    }
}

#[cfg(test)]
mod session_patch_tenant_scope_tests {
    //! 会话 PATCH 双端点租户 scope（BUG-78 A2）：PATCH /sessions/{id} 与
    //! PATCH /sessions/{id}/agent 必须与同文件 create/list/delete 同语义——
    //! request_tenant_ctx 解析请求租户并在 scope 内读写。修复前无 scope：
    //! get_session 恒以 default 租户查询——非 default 租户用户改自己会话查空
    //! （伪造内存回退 200，脱钩不落库），改 default 租户会话反而命中（跨租户写）。
    //! 修复后：非本租户/不存在 → 404 且零副作用；本租户 → 200 + 落库。

    use axum::{body::Body, http::Request, http::StatusCode};
    use tower::ServiceExt;

    use super::*;
    use agentos_core::traits::StorageBackend;
    use agentos_core::types::TenantContext;

    const SEED_PW: &str = "test-patch-tenant-pw-2026";
    const ALICE_ID: &str = "00000000-0000-0000-0000-0000000000a1";
    const ALICE_TENANT: &str = "t-alice";

    fn sqlite() -> std::sync::Arc<agentos_engine::SqliteStore> {
        std::sync::Arc::new(agentos_engine::SqliteStore::open_memory().unwrap())
    }

    async fn seed_user(
        store: &agentos_engine::SqliteStore,
        user_id: &str,
        username: &str,
        tenant_id: &str,
    ) -> String {
        let record = UserRecord {
            user_id: user_id.to_string(),
            username: username.to_string(),
            password: agentos_http::auth::hash_password(SEED_PW).unwrap(),
            email: None,
            // 写面角色闸（write_surface_auth）要求 admin——各租户的租户管理员
            role: "admin".to_string(),
            tenant_id: tenant_id.to_string(),
            created_at: chrono::Utc::now().to_rfc3339(),
            last_login_at: None,
            must_change_password: false,
        };
        store.create_user(&record).await.unwrap();
        agentos_http::auth::encode_token(
            agentos_http::auth::TokenType::Access,
            &agentos_http::auth::BuiltInUser::from(&record),
            3600,
        )
    }

    async fn seed_session(store: &agentos_engine::SqliteStore, thread_id: &str, tenant_id: &str) {
        // 主管道指针 + pipeline_sessions 映射一并落库：PATCH 切绑真值落点以
        // 持久真值为源（X5-F1 fail-closed），无主管道的会话 PATCH 即 503——
        // 本助手只供租户语义测试，种子须是健康会话。
        let pipeline_id = format!("pipe-{thread_id}");
        let rec = SessionRecord {
            thread_id: thread_id.to_string(),
            title: Some("原标题".to_string()),
            intent: None,
            current_state: "active".to_string(),
            agent_id: Some("origin_agent".to_string()),
            active_pipeline_id: Some(pipeline_id.clone()),
            pipeline_ids: vec![pipeline_id.clone()],
            metadata: Some(json!({"session_type": "main_pipeline"})),
            created_at: chrono::Utc::now().to_rfc3339(),
            updated_at: chrono::Utc::now().to_rfc3339(),
            last_active_at: None,
        };
        let link = store.link_pipeline_session(&pipeline_id, thread_id, tenant_id);
        if tenant_id == "default" {
            store.create_session(&rec).await.unwrap();
            link.await.unwrap();
        } else {
            agentos_tenant::scope(TenantContext::new(tenant_id, "seed"), async move {
                store.create_session(&rec).await?;
                link.await
            })
            .await
            .unwrap();
        }
    }

    async fn setup_router() -> (
        axum::Router,
        AppState,
        std::sync::Arc<agentos_engine::SqliteStore>,
    ) {
        let store = sqlite();
        let mut state = AppState::new();
        state.store = Some(store.clone());
        state.db = Some(store.clone());
        state.session = Some(std::sync::Arc::new(
            agentos_session::SessionCoordinator::new(),
        ));
        let router = crate::server::build_router(state.clone());
        (router, state, store)
    }

    async fn patch(
        router: &axum::Router,
        uri: String,
        token: &str,
        body: Value,
    ) -> (StatusCode, Value) {
        let resp = router
            .clone()
            .oneshot(
                Request::builder()
                    .method("PATCH")
                    .uri(uri)
                    .header("content-type", "application/json")
                    .header("authorization", format!("Bearer {token}"))
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        let status = resp.status();
        let bytes = axum::body::to_bytes(resp.into_body(), 16384).await.unwrap();
        let json: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
        (status, json)
    }

    #[tokio::test]
    async fn patch_rename_cross_tenant_returns_404_without_mutating() {
        // t-alice 用户 PATCH default 租户的会话 → 404，DB 记录原封不动。
        // 修复前：无 scope 恒按 default 查询 → 命中并跨租户改写（200）。
        let (router, _state, store) = setup_router().await;
        seed_session(&store, "t-def", "default").await;
        let alice_token = seed_user(&store, ALICE_ID, "alice", ALICE_TENANT).await;

        let (status, body) = patch(
            &router,
            "/api/v1/sessions/t-def".to_string(),
            &alice_token,
            json!({"intent": "越权改名"}),
        )
        .await;
        assert_eq!(status, StatusCode::NOT_FOUND, "跨租户改名必须 404: {body}");

        let rec = store.get_session("t-def").await.unwrap().unwrap();
        assert_eq!(rec.title.as_deref(), Some("原标题"), "DB 记录不得被改写");
    }

    #[tokio::test]
    async fn patch_agent_cross_tenant_returns_404_without_mutating() {
        // t-alice 用户 PATCH default 租户会话的 agent 绑定 → 404，
        // DB 记录 agent_id 与内存 registry 均原封不动。
        let (router, state, store) = setup_router().await;
        seed_session(&store, "t-def", "default").await;
        let alice_token = seed_user(&store, ALICE_ID, "alice", ALICE_TENANT).await;

        let (status, body) = patch(
            &router,
            "/api/v1/sessions/t-def/agent".to_string(),
            &alice_token,
            json!({"agent_id": "hijacked_agent"}),
        )
        .await;
        assert_eq!(status, StatusCode::NOT_FOUND, "跨租户绑定必须 404: {body}");

        let rec = store.get_session("t-def").await.unwrap().unwrap();
        assert_eq!(
            rec.agent_id.as_deref(),
            Some("origin_agent"),
            "DB agent_id 不得被跨租户改写"
        );
        let registry = state.session.as_ref().unwrap().registry();
        assert_ne!(
            registry.get_agent_for_thread("t-def").as_deref(),
            Some("hijacked_agent"),
            "404 路径不得写内存 registry"
        );
    }

    #[tokio::test]
    async fn patch_rename_in_request_tenant_persists() {
        // 两组有区分度输入：非 default 租户（t-alice）与 default 租户各自
        // PATCH 本租户会话 → 200 且落库到各自租户（修复前非 default 租户
        // 命中伪造回退 200，响应像成功但 DB 不落库）。
        let (router, _state, store) = setup_router().await;
        seed_session(&store, "t-alice-1", ALICE_TENANT).await;
        seed_session(&store, "t-def", "default").await;
        let alice_token = seed_user(&store, ALICE_ID, "alice", ALICE_TENANT).await;
        let admin_token = seed_user(&store, "u-admin-1", "root_admin", "default").await;

        // ① t-alice 改自己会话：200 + 落库在本租户
        let (status, body) = patch(
            &router,
            "/api/v1/sessions/t-alice-1".to_string(),
            &alice_token,
            json!({"intent": "改名后"}),
        )
        .await;
        assert_eq!(status, StatusCode::OK, "本租户改名应成功: {body}");
        let rec = agentos_tenant::scope(
            TenantContext::new(ALICE_TENANT, "verify"),
            store.get_session("t-alice-1"),
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(rec.title.as_deref(), Some("改名后"), "title 必须落库");
        assert_eq!(rec.intent.as_deref(), Some("改名后"));

        // ② default 租户改自己会话：200 + 落库（基线不回归）
        let (status, body) = patch(
            &router,
            "/api/v1/sessions/t-def".to_string(),
            &admin_token,
            json!({"intent": "默认租户新名"}),
        )
        .await;
        assert_eq!(status, StatusCode::OK, "default 租户改名应成功: {body}");
        let rec = store.get_session("t-def").await.unwrap().unwrap();
        assert_eq!(rec.title.as_deref(), Some("默认租户新名"));
    }

    #[tokio::test]
    async fn patch_agent_in_request_tenant_persists() {
        // agent 绑定同矩阵：本租户 PATCH → 200 + sessions 表落库 + registry 同步；
        // 两组租户输入（t-alice / default）行为一致。
        let (router, state, store) = setup_router().await;
        seed_session(&store, "t-alice-1", ALICE_TENANT).await;
        seed_session(&store, "t-def", "default").await;
        let alice_token = seed_user(&store, ALICE_ID, "alice", ALICE_TENANT).await;
        let admin_token = seed_user(&store, "u-admin-1", "root_admin", "default").await;

        let (status, body) = patch(
            &router,
            "/api/v1/sessions/t-alice-1/agent".to_string(),
            &alice_token,
            json!({"agent_id": "general_agent"}),
        )
        .await;
        assert_eq!(status, StatusCode::OK, "本租户绑定应成功: {body}");
        let rec = agentos_tenant::scope(
            TenantContext::new(ALICE_TENANT, "verify"),
            store.get_session("t-alice-1"),
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(
            rec.agent_id.as_deref(),
            Some("general_agent"),
            "agent_id 必须落库（本租户 scope 内）"
        );
        let registry = state.session.as_ref().unwrap().registry();
        assert_eq!(
            registry.get_agent_for_thread("t-alice-1").as_deref(),
            Some("general_agent"),
            "registry 热绑定同步"
        );

        let (status, body) = patch(
            &router,
            "/api/v1/sessions/t-def/agent".to_string(),
            &admin_token,
            json!({"agent_id": "general_agent"}),
        )
        .await;
        assert_eq!(status, StatusCode::OK, "default 租户绑定应成功: {body}");
        let rec = store.get_session("t-def").await.unwrap().unwrap();
        assert_eq!(rec.agent_id.as_deref(), Some("general_agent"));
    }
}
