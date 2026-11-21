// @feature: FP-0.2.内核API面 | @vision: V3 可嵌入 | @ci: rust-test
use super::*;
use crate::session_routes::MessageListQuery;
use agentos_core::traits::StorageBackend;
use axum::http::HeaderMap;

fn session_record(
    thread_id: &str,
    session_type: &str,
    pipeline_ids: Vec<String>,
) -> agentos_core::types::SessionRecord {
    agentos_core::types::SessionRecord {
        thread_id: thread_id.to_string(),
        title: Some(format!("标题-{thread_id}")),
        intent: Some("意图".to_string()),
        current_state: "active".to_string(),
        agent_id: Some("agentos".to_string()),
        active_pipeline_id: pipeline_ids.first().cloned(),
        pipeline_ids,
        metadata: Some(serde_json::json!({
            "session_type": session_type,
            "pinned": true,
        })),
        created_at: "2026-09-01T00:00:00+00:00".to_string(),
        updated_at: "2026-09-02T00:00:00+00:00".to_string(),
        last_active_at: None,
    }
}

// ── GET /api/v1/sessions（DB 成功路径 + 映射表补全）──────────────────

#[tokio::test]
async fn list_sessions_returns_seeded_rows_with_child_pipeline_merge() {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let dyn_store: Arc<dyn StorageBackend> = store.clone();
    dyn_store
        .create_session(&session_record(
            "t_main",
            "main_pipeline",
            vec!["p_main".into()],
        ))
        .await
        .unwrap();
    // 子管道映射（link 表）并入 pipeline_ids
    dyn_store
        .link_pipeline_session("p_child", "t_main", "default")
        .await
        .unwrap();
    // 非 main_pipeline 会话被 session_type 过滤
    dyn_store
        .create_session(&session_record("t_cli", "cli", vec![]))
        .await
        .unwrap();

    let mut state = AppState::new();
    state.store = Some(dyn_store);

    // 租户解析按真值走（K8 修订 ADR 2026-10-10）：播种 admin 并用绑定
    // 同源哈希的 token（admin 租户 = default，读面断言不受影响）。
    let mut auth_hdrs = axum::http::HeaderMap::new();
    auth_hdrs.insert(
        "authorization",
        seed_admin_token(&store).await.parse().unwrap(),
    );
    let resp = list_sessions_handler(axum::extract::State(state), auth_hdrs)
        .await
        .unwrap();
    assert_eq!(
        resp.0["total"], 1,
        "非 main_pipeline 会话被过滤: {}",
        resp.0
    );
    let thread = &resp.0["threads"][0];
    assert_eq!(thread["thread_id"], "t_main");
    assert_eq!(thread["title"], "标题-t_main");
    assert_eq!(thread["current_state"], "active");
    assert_eq!(thread["agent_id"], "agentos");
    assert_eq!(thread["metadata"]["pinned"], true);
    let pids: Vec<&str> = thread["pipeline_ids"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|p| p.as_str())
        .collect();
    assert!(pids.contains(&"p_main"), "主管道在列: {pids:?}");
    assert!(pids.contains(&"p_child"), "link 表子管道必须并入: {pids:?}");
    assert_eq!(thread["active_pipeline_id"], "p_main");
}

#[tokio::test]
async fn list_sessions_without_store_falls_back_to_memory_registry() {
    let mut state = AppState::new();
    state.session = Some(Arc::new(agentos_session::SessionCoordinator::new()));
    let registry = state.session.as_ref().unwrap().registry().clone();
    registry.register_thread("thread-mem-cov", "u-mem");
    registry.register_thread_pipeline("thread-mem-cov", "pipe-mem");

    let resp = list_sessions_handler(axum::extract::State(state), HeaderMap::new())
        .await
        .unwrap();
    assert_eq!(resp.0["total"], 1);
    let thread = &resp.0["threads"][0];
    assert_eq!(thread["thread_id"], "thread-mem-cov");
    assert_eq!(thread["pipeline_ids"][0], "pipe-mem");
    assert_eq!(thread["active_pipeline_id"], "pipe-mem");
    assert_eq!(thread["metadata"]["user_id"], "u-mem");
}

// ── POST /api/v1/sessions（用户解析与 metadata 默认合并）──────────────

#[tokio::test]
async fn create_session_merges_metadata_defaults_and_persists() {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let dyn_store: Arc<dyn StorageBackend> = store.clone();
    let mut state = AppState::new();
    state.store = Some(dyn_store.clone());
    state.session = Some(Arc::new(agentos_session::SessionCoordinator::new()));

    // 无 token + body user_id → 显式降级使用 body 值（可伪造，warn 标记）
    let resp = create_session_handler(
        axum::extract::State(state.clone()),
        HeaderMap::new(),
        axum::Json(serde_json::json!({
            "title": "T1",
            "user_id": "body-user",
            "metadata": {"custom": 1}
        })),
    )
    .await
    .unwrap();

    let thread_id = resp.0["thread_id"].as_str().unwrap().to_string();
    assert_eq!(resp.0["title"], "T1");
    assert_eq!(resp.0["intent"], "T1", "title 缺 intent 时回退 title");
    assert_eq!(resp.0["pipeline_ids"].as_array().unwrap().len(), 1);
    assert_eq!(resp.0["metadata"]["custom"], 1, "body 元数据保留");
    assert_eq!(
        resp.0["metadata"]["session_type"], "main_pipeline",
        "缺失默认必须补齐（列表过滤依赖）"
    );
    assert_eq!(resp.0["metadata"]["user_id"], "body-user");

    // 落库核对：双写 sessions 表 + registry 登记
    let rec = dyn_store.get_session(&thread_id).await.unwrap().unwrap();
    assert_eq!(rec.title.as_deref(), Some("T1"));
    assert_eq!(
        rec.active_pipeline_id.as_deref(),
        resp.0["active_pipeline_id"].as_str()
    );
    let registry = state.session.as_ref().unwrap().registry().clone();
    assert!(
        registry.get_pipeline_for_thread(&thread_id).is_some(),
        "thread→pipeline 注册表必须登记"
    );

    // 无 body user_id → anonymous 兜底
    let resp = create_session_handler(
        axum::extract::State(state),
        HeaderMap::new(),
        axum::Json(serde_json::json!({})),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["title"], serde_json::Value::Null);
    assert_eq!(resp.0["intent"], serde_json::Value::Null);
    assert_eq!(
        resp.0["metadata"]["user_id"], "anonymous",
        "无 token 无 body 用户 → anonymous"
    );
    assert_eq!(
        resp.0["metadata"]["session_type"], "main_pipeline",
        "body 无 metadata 也补默认"
    );
}

// ── PATCH /api/v1/sessions/{id}（重命名）─────────────────────────────

#[tokio::test]
async fn update_session_renames_db_record_and_404s_when_absent() {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let dyn_store: Arc<dyn StorageBackend> = store.clone();
    dyn_store
        .create_session(&session_record(
            "t_upd",
            "main_pipeline",
            vec!["p_upd".into()],
        ))
        .await
        .unwrap();
    let mut state = AppState::new();
    state.store = Some(dyn_store.clone());
    state.session = Some(Arc::new(agentos_session::SessionCoordinator::new()));

    // DB 命中：title/intent 同步更新 + updated_at 前移
    let resp = update_session_handler(
        axum::extract::State(state.clone()),
        HeaderMap::new(),
        axum::extract::Path("t_upd".to_string()),
        axum::Json(serde_json::json!({"intent": "新名字"})),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["thread_id"], "t_upd");
    assert_eq!(resp.0["title"], "新名字");
    assert_eq!(resp.0["intent"], "新名字");
    let rec = dyn_store.get_session("t_upd").await.unwrap().unwrap();
    assert_eq!(rec.title.as_deref(), Some("新名字"));
    assert_eq!(rec.intent.as_deref(), Some("新名字"));
    assert_eq!(
        rec.updated_at, resp.0["updated_at"],
        "响应 updated_at 与落库一致"
    );
    assert_ne!(rec.updated_at, "2026-09-02T00:00:00+00:00");

    // 无 intent 的 PATCH：只碰 updated_at，title 保持
    let resp = update_session_handler(
        axum::extract::State(state.clone()),
        HeaderMap::new(),
        axum::extract::Path("t_upd".to_string()),
        axum::Json(serde_json::json!({})),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["title"], "新名字", "无 intent 不得清标题");

    // DB 未命中：404（不回退内存构造伪造 200；与 list「无记录不出口」同语义）
    state
        .session
        .as_ref()
        .unwrap()
        .registry()
        .register_thread_agent("t-ghost", "ag_ghost");
    let resp = update_session_handler(
        axum::extract::State(state),
        HeaderMap::new(),
        axum::extract::Path("t-ghost".to_string()),
        axum::Json(serde_json::json!({"intent": "ghost-name"})),
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

// ── PATCH /api/v1/sessions/{id}/agent（DB 未命中回退）────────────────

#[tokio::test]
async fn update_session_agent_falls_back_to_registry_state_without_db_record() {
    let mut state = AppState::new();
    state.session = Some(Arc::new(agentos_session::SessionCoordinator::new()));
    let registry = state.session.as_ref().unwrap().registry().clone();
    registry.register_thread_pipeline("t-fb", "p-fb");

    let resp = update_session_agent_handler(
        axum::extract::State(state),
        HeaderMap::new(),
        axum::extract::Path("t-fb".to_string()),
        axum::Json(serde_json::json!({"agent_id": "ag_fb"})),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["agent_id"], "ag_fb");
    assert_eq!(resp.0["pipeline_ids"][0], "p-fb");
    assert_eq!(resp.0["active_pipeline_id"], "p-fb");
    assert_eq!(resp.0["current_state"], "active");
    // body 缺 agent_id → 默认 agentos
    let resp = update_session_agent_handler(
        axum::extract::State(AppState::new()),
        HeaderMap::new(),
        axum::extract::Path("t-x".to_string()),
        axum::Json(serde_json::json!({})),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["agent_id"], "agentos");
}

// ── GET /api/v1/sessions/{id}/messages（消息映射全字段）───────────────

fn seed_message_ops(
    store: &Arc<agentos_engine::SqliteStore>,
    pipeline_id: &str,
    ops: &[serde_json::Value],
) {
    store
        .apply_messages_ops_to_table(pipeline_id, "default", ops)
        .unwrap();
}

#[tokio::test]
async fn list_messages_maps_tool_envelope_reasoning_and_metadata() {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    store
        .record_run_start(
            "pipe_msgs_cov",
            "default",
            "run_msgs_cov",
            "cfg",
            "2026-10-15T00:00:00+00:00",
        )
        .unwrap();
    seed_message_ops(
        &store,
        "pipe_msgs_cov",
        &[
            serde_json::json!({
                "op": "set", "seq": 0, "_run_id": "run_msgs_cov",
                "msg": {"role": "user", "content": "问题",
                        "metadata": {"client_message_id": "cmid-1"}}
            }),
            serde_json::json!({
                "op": "set", "seq": 1, "_run_id": "run_msgs_cov",
                "msg": {"role": "assistant", "content": "回答",
                        "reasoning_content": "思考过程…",
                        "tool_calls": [
                            {"id": "c1", "type": "function",
                             "function": {"name": "f", "arguments": "{}"}}
                        ]}
            }),
            serde_json::json!({
                "op": "set", "seq": 2, "_run_id": "run_msgs_cov",
                "msg": {"role": "tool", "content": "结果文本", "tool_call_id": "c1",
                        "tool_result": {"call_id": "c1", "tool_name": "f",
                                        "success": false, "error": "boom",
                                        "data": {"d": 1}, "duration_ms": 12.5,
                                        "metadata": {"container_task_id": "ct7"}}}
            }),
            serde_json::json!({
                "op": "set", "seq": 3, "_run_id": "run_msgs_cov",
                "msg": {"role": "tool", "content": "Error: 磁盘满"}
            }),
        ],
    );

    let mut state = AppState::new();
    state.store = Some(store.clone());
    state.db = Some(store.clone());

    let resp = list_session_messages_handler(
        axum::extract::State(state),
        HeaderMap::new(),
        axum::extract::Path("pipe_msgs_cov".to_string()),
        axum::extract::Query(MessageListQuery {
            pipeline_run_id: Some("pipe_msgs_cov".to_string()),
            before_sequence: None,
            after_sequence: None,
            limit: None,
        }),
    )
    .await
    .unwrap();
    let msgs = resp.0["messages"].as_array().unwrap();
    assert_eq!(resp.0["total"], 4);
    assert_eq!(resp.0["has_more"], serde_json::json!(false));
    let by_seq = |s: u32| {
        msgs.iter()
            .find(|m| m["sequence"] == s)
            .unwrap_or_else(|| panic!("缺 seq={s}"))
    };

    // user：metadata.client_message_id 原样回显（幂等对账键）
    let m0 = by_seq(0);
    assert_eq!(m0["role"], "user");
    assert_eq!(m0["content"], "问题");
    assert_eq!(m0["metadata"]["client_message_id"], "cmid-1");
    assert_eq!(m0["status"], "completed");
    assert!(m0["id"].is_string());
    assert!(m0["timestamp"].is_string());

    // assistant：tool_calls 反序列化 + reasoning_content
    let m1 = by_seq(1);
    assert_eq!(m1["toolCalls"][0]["id"], "c1");
    assert_eq!(m1["reasoningContent"], "思考过程…");

    // tool（envelope 失败）：status/error/toolCallId/结构化结果字段
    let m2 = by_seq(2);
    assert_eq!(m2["status"], "failed");
    assert_eq!(m2["error"], "boom");
    assert_eq!(m2["toolCallId"], "c1");
    assert_eq!(m2["toolName"], "f");
    assert_eq!(m2["toolResultData"]["d"], 1);
    assert_eq!(m2["toolDurationMs"], 12.5);
    assert_eq!(m2["containerTaskId"], "ct7");

    // tool（content 前缀失败、无 envelope）：状态按前缀判定
    let m3 = by_seq(3);
    assert_eq!(m3["status"], "failed");
    assert_eq!(m3["error"], "磁盘满");
    assert!(m3.get("toolResultData").is_none());

    // 游标分页：after_sequence=2 + limit=2 → 只剩 seq 3，1 < limit 无更多
    let mut state = AppState::new();
    state.store = Some(store.clone());
    state.db = Some(store.clone());
    let resp = list_session_messages_handler(
        axum::extract::State(state),
        HeaderMap::new(),
        axum::extract::Path("pipe_msgs_cov".to_string()),
        axum::extract::Query(MessageListQuery {
            pipeline_run_id: Some("pipe_msgs_cov".to_string()),
            before_sequence: None,
            after_sequence: Some(2),
            limit: Some(2),
        }),
    )
    .await
    .unwrap();
    let msgs = resp.0["messages"].as_array().unwrap();
    assert_eq!(msgs.len(), 1, "after_sequence=2 应只剩 seq 3");
    assert_eq!(msgs[0]["sequence"], 3);
    assert_eq!(resp.0["has_more"], serde_json::json!(false), "1 < limit 2");
}

#[tokio::test]
async fn list_messages_falls_back_to_active_pipeline_and_handles_no_store() {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    store
        .record_run_start(
            "pipe_active_cov",
            "default",
            "run_fb_cov",
            "cfg",
            "2026-10-15T00:00:00+00:00",
        )
        .unwrap();
    seed_message_ops(
        &store,
        "pipe_active_cov",
        &[serde_json::json!({
            "op": "set", "seq": 0, "_run_id": "run_fb_cov",
            "msg": {"role": "user", "content": "in-active"}
        })],
    );
    let dyn_store: Arc<dyn StorageBackend> = store.clone();
    dyn_store
        .create_session(&session_record(
            "t_fb",
            "main_pipeline",
            vec!["pipe_active_cov".into()],
        ))
        .await
        .unwrap();

    // 不带 pipeline_run_id：按会话 active_pipeline_id 解析目标管道
    let mut state = AppState::new();
    state.store = Some(dyn_store.clone());
    state.db = Some(store);
    let resp = list_session_messages_handler(
        axum::extract::State(state),
        HeaderMap::new(),
        axum::extract::Path("t_fb".to_string()),
        axum::extract::Query(MessageListQuery {
            pipeline_run_id: None,
            before_sequence: None,
            after_sequence: None,
            limit: None,
        }),
    )
    .await
    .unwrap();
    let msgs = resp.0["messages"].as_array().unwrap();
    assert_eq!(msgs.len(), 1, "thread→active_pipeline 回退: {}", resp.0);
    assert_eq!(msgs[0]["content"], "in-active");
    assert_eq!(msgs[0]["thread_id"], "t_fb", "回填路径 id 满足前端 mapper");

    // store 未配置 → 空历史形状（不报错）
    let resp = list_session_messages_handler(
        axum::extract::State(AppState::new()),
        HeaderMap::new(),
        axum::extract::Path("t_any".to_string()),
        axum::extract::Query(MessageListQuery {
            pipeline_run_id: None,
            before_sequence: None,
            after_sequence: None,
            limit: None,
        }),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["messages"].as_array().unwrap().len(), 0);
    assert_eq!(resp.0["total"], 0);
    assert_eq!(resp.0["has_more"], serde_json::json!(false));
}

// ── GET /api/v1/sessions/schema（内置 + 插件聚合）────────────────────

#[tokio::test]
async fn sessions_schema_aggregates_enabled_plugin_thread_fields() {
    let mk = |id: &str, thread_fields: serde_json::Value| {
        serde_json::from_value::<agentos_core::traits::PluginManifest>(serde_json::json!({
            "id": id, "name": id, "version": "1.0.0",
            "plugin_type": "system", "language": "python",
            "host_type": "sidecar", "entry": "x",
            "contributes": {"thread_fields": thread_fields},
            "capabilities": {},
        }))
        .unwrap()
    };
    let mut state = AppState::new();
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![
        mk(
            "p_iso_on",
            serde_json::json!([
                {"name": "workspace", "label": "工作区", "type": "string"},
                {"label": "缺 name 不构成字段"}
            ]),
        ),
        mk("p_iso_off", serde_json::json!([{"name": "off_field"}])),
    ]));
    state.enabled_plugin_ids =
        Arc::new(tokio::sync::RwLock::new(std::collections::HashSet::from([
            "p_iso_on".to_string(),
        ])));

    let resp = sessions_schema_handler(axum::extract::State(state)).await;
    let fields = resp.0["fields"].as_array().unwrap();
    let names: Vec<&str> = fields.iter().filter_map(|f| f["name"].as_str()).collect();
    assert_eq!(
        names,
        vec!["title", "intent", "workspace"],
        "内置字段在前 + 仅 enabled 插件 + 仅带 name 的声明: {names:?}"
    );
}
