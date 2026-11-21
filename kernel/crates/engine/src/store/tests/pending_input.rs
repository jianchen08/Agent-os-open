use super::*;
use agentos_core::types::PendingInputSource;

// ── 分层持久化投影测试 ──────────────────────────────────────

/// upsert_state_field 幂等 + load_pipeline_state 往返。
#[tokio::test]
async fn test_upsert_and_load_pipeline_state() {
    let store = SqliteStore::open_memory().unwrap();
    let pid = "pipe_state_test";
    // 首次 upsert
    store
        .upsert_state_field(pid, "default", "track.total_tokens", &json!(150))
        .unwrap();
    // 再次 upsert 覆盖（累计语义）
    store
        .upsert_state_field(pid, "default", "track.total_tokens", &json!(300))
        .unwrap();
    store
        .upsert_state_field(pid, "default", "track.llm_usage", &json!({"prompt": 10}))
        .unwrap();

    let loaded = store.load_pipeline_state(pid, "default").unwrap();
    assert_eq!(
        loaded.get("track.total_tokens"),
        Some(&json!(300)),
        "应取最新覆盖值"
    );
    assert_eq!(loaded.get("track.llm_usage"), Some(&json!({"prompt": 10})));
    assert_eq!(loaded.len(), 2, "应有 2 个字段");
}

// ── 域11：pending 输入队列（ADR-2026-08-26）──────────────────────

/// 构造一条 pending 输入（测试辅助）。
fn pending_input(id: &str, pid: &str, content: &str, created: &str) -> PendingInputRecord {
    PendingInputRecord {
        id: id.to_string(),
        tenant_id: "default".to_string(),
        created_at: created.to_string(),
        request: agentos_core::types::DispatchRequest {
            thread_id: format!("thread-{pid}"),
            user_id: "u1".to_string(),
            pipeline_id: pid.to_string(),
            content: content.to_string(),
            client_message_id: String::new(),
            source: PendingInputSource::User,
            execution_context: None,
            state_overlay: Some(json!({"task.goal": content})),
            pipeline_config_id: None,
        },
    }
}

/// 入队/列出/弹出往返 + FIFO 序（created_at, id 升序）。
#[tokio::test]
async fn test_pending_inputs_enqueue_list_pop_fifo() {
    let store = SqliteStore::open_memory().unwrap();
    let pid = "pipe_pending_1";
    // 逆序入队（created_at 升序时队首应为 p1）
    store
        .enqueue_pending_input(
            "default",
            &pending_input("in_3", pid, "第三条", "2026-08-26T03:00:00Z"),
        )
        .unwrap();
    store
        .enqueue_pending_input(
            "default",
            &pending_input("in_1", pid, "第一条", "2026-08-26T01:00:00Z"),
        )
        .unwrap();
    store
        .enqueue_pending_input(
            "default",
            &pending_input("in_2", pid, "第二条", "2026-08-26T02:00:00Z"),
        )
        .unwrap();

    let listed = store.list_pending_inputs("default", pid).unwrap();
    let ids: Vec<&str> = listed.iter().map(|i| i.id.as_str()).collect();
    assert_eq!(
        ids,
        vec!["in_1", "in_2", "in_3"],
        "FIFO 序 = created_at 升序"
    );
    assert_eq!(listed[0].request.content, "第一条");
    assert_eq!(listed[0].request.source, PendingInputSource::User);
    assert_eq!(
        listed[0].request.state_overlay,
        Some(json!({"task.goal": "第一条"})),
        "overlay 往返保真"
    );

    // pop 取队首并删除（消费瞬态）
    let popped = store.pop_pending_input("default", pid).unwrap();
    assert_eq!(popped.unwrap().id, "in_1", "pop 取 FIFO 队首");
    let remaining = store.list_pending_inputs("default", pid).unwrap();
    assert_eq!(
        remaining.iter().map(|i| i.id.as_str()).collect::<Vec<_>>(),
        vec!["in_2", "in_3"],
        "pop 后剩余两条"
    );
}

/// pipeline_config_id 往返：显式指定经落库往返保留（链忙排队后消费仍用
/// 原配置）；缺省 None 往返保持 None（= autonomous，不产生假指定）。
#[tokio::test]
async fn test_pending_input_roundtrips_pipeline_config_id() {
    let store = SqliteStore::open_memory().unwrap();
    // 显式指定：入队 → pop 往返保真
    let mut explicit = pending_input(
        "in_pcid_1",
        "pipe_pcid_a",
        "指定配置",
        "2026-09-25T01:00:00Z",
    );
    explicit.request.pipeline_config_id = Some("custom_pipe".to_string());
    store.enqueue_pending_input("default", &explicit).unwrap();
    let got = store
        .pop_pending_input("default", "pipe_pcid_a")
        .unwrap()
        .expect("显式条目应可弹出");
    assert_eq!(
        got.request.pipeline_config_id.as_deref(),
        Some("custom_pipe"),
        "管道配置 id 必须经持久化往返保留（排队后消费仍用原配置）"
    );

    // 缺省：None 入队 → None 出队（旧客户端行为不变）
    let default_rec = pending_input("in_pcid_2", "pipe_pcid_b", "缺省", "2026-09-25T02:00:00Z");
    store
        .enqueue_pending_input("default", &default_rec)
        .unwrap();
    let got = store
        .pop_pending_input("default", "pipe_pcid_b")
        .unwrap()
        .expect("缺省条目应可弹出");
    assert!(
        got.request.pipeline_config_id.is_none(),
        "缺省往返必须保持 None，不得伪造指定: {:?}",
        got.request.pipeline_config_id
    );

    // list 路径同样保真（等待窗口列表视图）
    let mut explicit2 = pending_input(
        "in_pcid_3",
        "pipe_pcid_c",
        "列表视图",
        "2026-09-25T03:00:00Z",
    );
    explicit2.request.pipeline_config_id = Some("other_pipe".to_string());
    store.enqueue_pending_input("default", &explicit2).unwrap();
    let listed = store.list_pending_inputs("default", "pipe_pcid_c").unwrap();
    assert_eq!(
        listed[0].request.pipeline_config_id.as_deref(),
        Some("other_pipe")
    );
}

/// 空队列 pop 返回 None；不同租户/管道隔离。
#[tokio::test]
async fn test_pending_inputs_empty_and_isolation() {
    let store = SqliteStore::open_memory().unwrap();
    assert!(
        store
            .pop_pending_input("default", "pipe_empty")
            .unwrap()
            .is_none(),
        "空队列 pop 返回 None"
    );
    store
        .enqueue_pending_input(
            "default",
            &pending_input("a1", "pipe_a", "A", "2026-08-26T01:00:00Z"),
        )
        .unwrap();
    assert!(
        store
            .pop_pending_input("other_tenant", "pipe_a")
            .unwrap()
            .is_none(),
        "跨租户不可见"
    );
    assert!(
        store
            .pop_pending_input("default", "pipe_b")
            .unwrap()
            .is_none(),
        "跨管道不可见"
    );
}

/// 同 id 重复入队幂等（INSERT OR IGNORE）。
#[tokio::test]
async fn test_pending_inputs_enqueue_idempotent() {
    let store = SqliteStore::open_memory().unwrap();
    let pid = "pipe_pending_idem";
    let rec = pending_input("dup1", pid, "原始", "2026-08-26T01:00:00Z");
    store.enqueue_pending_input("default", &rec).unwrap();
    store.enqueue_pending_input("default", &rec).unwrap();
    let listed = store.list_pending_inputs("default", pid).unwrap();
    assert_eq!(listed.len(), 1, "同 id 重复入队不产生重复条目");
}

/// update/delete/clear 语义：存在→生效；不存在→Ok(false)/Ok(0)。
#[tokio::test]
async fn test_pending_inputs_update_delete_clear() {
    let store = SqliteStore::open_memory().unwrap();
    let pid = "pipe_pending_mut";
    store
        .enqueue_pending_input(
            "default",
            &pending_input("m1", pid, "旧内容", "2026-08-26T01:00:00Z"),
        )
        .unwrap();
    store
        .enqueue_pending_input(
            "default",
            &pending_input("m2", pid, "另一条", "2026-08-26T02:00:00Z"),
        )
        .unwrap();

    // 修改
    assert!(
        store
            .update_pending_input_content("default", pid, "m1", "新内容")
            .unwrap(),
        "存在的条目更新返回 true"
    );
    let listed = store.list_pending_inputs("default", pid).unwrap();
    assert_eq!(listed[0].request.content, "新内容", "update 覆盖 content");
    assert_eq!(listed[0].id, "m1", "update 不改变 FIFO 位置");
    assert!(
        !store
            .update_pending_input_content("default", pid, "ghost", "x")
            .unwrap(),
        "不存在条目 update 返回 false"
    );

    // 删除
    assert!(
        store.delete_pending_input("default", pid, "m2").unwrap(),
        "删除存在条目返回 true"
    );
    assert!(
        !store.delete_pending_input("default", pid, "m2").unwrap(),
        "删除不存在条目返回 false"
    );

    // 清空
    assert_eq!(
        store.clear_pending_inputs("default", pid).unwrap(),
        1,
        "清空返回删除条数"
    );
    assert!(
        store
            .list_pending_inputs("default", pid)
            .unwrap()
            .is_empty(),
        "清空后队列空"
    );
}

/// save_checkpoint / load_latest_checkpoint 往返 + 取最新。
#[tokio::test]
async fn test_save_and_load_checkpoint() {
    let store = SqliteStore::open_memory().unwrap();
    let pid = "pipe_ckpt_test";
    store
        .save_checkpoint(pid, "default", 5, &json!({"messages": [], "step": 5}))
        .unwrap();
    store
        .save_checkpoint(
            pid,
            "default",
            10,
            &json!({"messages": [{"role":"user","content":"hi"}], "step": 10}),
        )
        .unwrap();

    let latest = store.load_latest_checkpoint(pid, "default").unwrap();
    assert!(latest.is_some());
    let (step_no, state) = latest.unwrap();
    assert_eq!(step_no, 10, "应取 step_no 最大的 checkpoint");
    assert_eq!(state["step"], json!(10));
}

#[tokio::test]
async fn checkpoint_strips_control_state_keys_including_should_stop() {
    // 控制状态键契约：should_stop/ended/suspended/router.stop_reason 同属
    // per-run 控制键（署名与终止请求同写同消），checkpoint 瘦身剥离——
    // 冷恢复不得复活上一 run 的终止请求与署名。本测试裸 store（未注入
    // 声明键）只验内核自有键；插件语义 per-run 键
    // （conversation_mode/core_type/core_plugin 等）由 manifest
    // state.volatile_keys 声明、经 set_declared_volatile_keys 注入后与
    // 内核自有键在同一剥离点剥离
    // （见 pipeline_loop::tests::run_end_checkpoint_strips_declared_volatile_keys）。
    let store = SqliteStore::open_memory().unwrap();
    store
        .save_checkpoint(
            "pipe_ctrl",
            "default",
            3,
            &json!({
                "task.status": "running",
                "should_stop": true,
                "ended": true,
                "suspended": true,
                "router.stop_reason": "budget_exhausted"
            }),
        )
        .unwrap();
    let (_, state) = store
        .load_latest_checkpoint("pipe_ctrl", "default")
        .unwrap()
        .unwrap();
    for key in ["should_stop", "ended", "suspended", "router.stop_reason"] {
        assert!(state.get(key).is_none(), "{key} 不得残留进 checkpoint");
    }
    assert_eq!(state["task.status"], json!("running"), "持久键不受瘦身影响");
}

// ── A7：槽位 blob 解码的「合法缺失 vs 损坏」区分 ──

#[test]
fn decode_slot_message_distinguishes_missing_from_corruption() {
    // blob_id NULL：合法缺失（无指针）→ 空对象，不算损坏
    let (v, reason) = decode_slot_message(None, None);
    assert!(v.as_object().is_some_and(|o| o.is_empty()));
    assert!(reason.is_none(), "NULL blob_id 不得记为损坏");

    // 有 blob_id 但 blob 行缺失 → 损坏（降级 + 原因）
    let (v, reason) = decode_slot_message(Some("b_missing"), None);
    assert!(v.as_object().is_some_and(|o| o.is_empty()));
    assert!(reason.unwrap().contains("blob 行缺失"));

    // 非 UTF-8 → 损坏
    let (_, reason) = decode_slot_message(Some("b_utf8"), Some(&[0xff, 0xfe]));
    assert!(reason.unwrap().contains("UTF-8"));

    // JSON 解析失败 → 损坏
    let (_, reason) = decode_slot_message(Some("b_badjson"), Some(b"{not json"));
    assert!(reason.unwrap().contains("JSON"));

    // 正常 JSON → 原样返回，无损坏
    let (v, reason) = decode_slot_message(Some("b_ok"), Some(br#"{"role":"user"}"#));
    assert_eq!(v["role"], json!("user"));
    assert!(reason.is_none());
}

// ── 表形状与 v1→v2 迁移（ADR-2026-11-17）───────────────────────────

/// 表形状闸：pending 行 = 队列信封列 + request 单列（6 列固定）。
/// 旧 15 列强类型形态复活即红（新增查询需求应加索引/投影，不加业务列）。
#[test]
fn pending_table_is_envelope_plus_request_only() {
    let store = SqliteStore::open_memory().unwrap();
    let conn = store.conn.lock();
    let mut stmt = conn
        .prepare("SELECT name FROM pragma_table_info('pipeline_pending_inputs')")
        .unwrap();
    let mut cols: Vec<String> = stmt
        .query_map([], |row| row.get::<_, String>(0))
        .unwrap()
        .map(|r| r.unwrap())
        .collect();
    cols.sort();
    assert_eq!(
        cols,
        vec![
            "client_message_id",
            "created_at",
            "id",
            "pipeline_id",
            "request",
            "tenant_id",
        ],
        "pending 表只允许信封列 + request（业务载荷在 request JSON 内）"
    );
    let version: i64 = conn
        .query_row("PRAGMA user_version", [], |r| r.get(0))
        .unwrap();
    assert_eq!(
        version, SCHEMA_VERSION,
        "新库打开即盖 v2 版本章（旧内核打开 v2 库必须拒绝）"
    );
}

/// v1 → v2 迁移：旧 15 列行 → request 单列，且
/// legacy `agent_id` / `thinking_strength` 非空值并入 `state_overlay`
/// 对应键（排队中的用户选择不因列收窄而丢失）；幂等（第二次直接跳过）。
#[test]
fn migrate_pending_inputs_backfills_request_and_preserves_legacy_choices() {
    let conn = rusqlite::Connection::open_in_memory().unwrap();
    conn.execute_batch(
        "CREATE TABLE pipeline_pending_inputs (
            id TEXT PRIMARY KEY, pipeline_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL, content TEXT NOT NULL, thread TEXT NOT NULL,
            source TEXT NOT NULL, agent_id TEXT NOT NULL DEFAULT 'agentos',
            route_id TEXT NOT NULL DEFAULT '', thinking_strength TEXT NOT NULL DEFAULT '',
            client_message_id TEXT NOT NULL DEFAULT '', execution_context TEXT,
            state_overlay TEXT, pipeline_config_id TEXT, created_at TEXT NOT NULL);
         CREATE INDEX idx_pending_pipeline
            ON pipeline_pending_inputs(tenant_id, pipeline_id, created_at, id);
         INSERT INTO pipeline_pending_inputs
            (id, pipeline_id, tenant_id, user_id, content, thread, source, agent_id,
             route_id, thinking_strength, client_message_id, execution_context,
             state_overlay, pipeline_config_id, created_at)
         VALUES
            ('p_old1', 'pipe_old', 'default', 'u1', '旧消息', 'T-old', '\"user\"',
             'persona_a', 'pipe_old', '{\"reasoning_effort\":\"max\"}', 'cm-old',
             '{\"workspace\":{\"mode\":\"plain\"}}', '{\"task.goal\":\"g1\"}',
             'autonomous', '2026-11-01T00:00:00Z');",
    )
    .unwrap();

    super::super::schema::migrate_pending_input_dispatch_request(&conn).unwrap();
    // 幂等：再跑一次不报错、不重复改动
    super::super::schema::migrate_pending_input_dispatch_request(&conn).unwrap();

    let mut stmt = conn
        .prepare("SELECT request FROM pipeline_pending_inputs WHERE id='p_old1'")
        .unwrap();
    let raw: String = stmt.query_row([], |r| r.get(0)).unwrap();
    let request: DispatchRequest = serde_json::from_str(&raw).expect("回填 request 必须可解析");
    assert_eq!(request.pipeline_id, "pipe_old", "路由键取 route_id 列");
    assert_eq!(request.thread_id, "T-old");
    assert_eq!(request.user_id, "u1");
    assert_eq!(request.content, "旧消息");
    assert_eq!(request.client_message_id, "cm-old");
    assert_eq!(request.source, PendingInputSource::User);
    assert_eq!(request.pipeline_config_id.as_deref(), Some("autonomous"));
    let overlay = request.state_overlay.expect("legacy 选择应并入 overlay");
    assert_eq!(
        overlay["agent.id"], "persona_a",
        "legacy agent_id 并入 agent.id"
    );
    assert_eq!(
        overlay["thinking_strength"], "{\"reasoning_effort\":\"max\"}",
        "legacy thinking_strength 并入同名声明键"
    );
    assert_eq!(overlay["task.goal"], "g1", "原 overlay 键保留");

    // 旧列已收窄，只剩 6 列
    let count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM pragma_table_info('pipeline_pending_inputs')",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 6, "迁移后表只保留信封 + request 列");
}
