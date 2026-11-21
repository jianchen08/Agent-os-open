use super::*;
use agentos_core::types::PendingInputSource;

// ── 读路径：slot_row_to_record 的 tool 状态判定（envelope 权威 + 内容兜底）──

/// tool 消息的状态提取：envelope 优先（success 真/假），无 envelope 时
/// 按 `Error: ` 前缀兜底；非 tool 角色恒 None。envelope 假且无 error 字段
/// 时回退 content 前缀提取。
#[test]
fn slot_row_to_record_tool_status_precedence() {
    let rec = |msg: &serde_json::Value| {
        slot_row_to_record(
            msg,
            0,
            "m1".to_string(),
            Some("b1".to_string()),
            "2026-01-01T00:00:00Z".to_string(),
            Some("pipe".to_string()),
            Some("run".to_string()),
        )
    };

    // envelope success=false 且带 error：error 取 envelope 原文
    let failed = rec(&json!({
        "role": "tool",
        "content": "whatever",
        "tool_result": {"success": false, "error": "envelope error"}
    }));
    assert_eq!(failed.status.as_deref(), Some("failed"));
    assert_eq!(failed.error.as_deref(), Some("envelope error"));

    // envelope success=false 无 error：回退 content "Error: " 前缀
    let failed_fallback = rec(&json!({
        "role": "tool",
        "content": "Error: content error",
        "tool_result": {"success": false}
    }));
    assert_eq!(failed_fallback.status.as_deref(), Some("failed"));
    assert_eq!(failed_fallback.error.as_deref(), Some("content error"));

    // envelope success=true 压过 content 前缀（结构化真相优先）
    let ok = rec(&json!({
        "role": "tool",
        "content": "Error: 文字像失败",
        "tool_result": {"success": true}
    }));
    assert_eq!(ok.status.as_deref(), Some("completed"));
    assert!(ok.error.is_none(), "envelope 成功不得残留 error");

    // 无 envelope：content "Error: " 前缀判失败（旧语义兜底）
    let legacy_fail = rec(&json!({"role": "tool", "content": "Error: legacy"}));
    assert_eq!(legacy_fail.status.as_deref(), Some("failed"));
    assert_eq!(legacy_fail.error.as_deref(), Some("legacy"));
    let legacy_ok = rec(&json!({"role": "tool", "content": "normal output"}));
    assert_eq!(legacy_ok.status.as_deref(), Some("completed"));
    assert!(legacy_ok.error.is_none());

    // 非 tool 角色：状态与 error 均不出口
    let user = rec(&json!({"role": "user", "content": "Error: 这只是正文"}));
    assert!(
        user.status.is_none() && user.error.is_none(),
        "非 tool 无状态"
    );

    // 非 tool 消息 blob status 同源透出（缺陷B，台账 R1）：中断/错误半截
    // assistant 消息 blob 带 status=interrupted/error（llm_core 落库原文，
    // WS 热路径 message_status_from_blob 同源）——REST 冷路径读 blob 同键，
    // 冷热读面同构；正常消息 blob 无 status 键 → None（handler 回退
    // completed，行为不变）。
    let interrupted = rec(&json!({
        "role": "assistant",
        "content": "半截内容",
        "status": "interrupted"
    }));
    assert_eq!(
        interrupted.status.as_deref(),
        Some("interrupted"),
        "中断半截 assistant：blob status 透出"
    );
    let errored = rec(&json!({
        "role": "assistant",
        "content": "出错前内容",
        "status": "error"
    }));
    assert_eq!(errored.status.as_deref(), Some("error"));
    assert!(
        errored.error.is_none(),
        "blob status 透出不伪造 error（error 仍是 tool 域字段）"
    );
    let normal = rec(&json!({"role": "assistant", "content": "完整回复"}));
    assert!(
        normal.status.is_none(),
        "正常消息 blob 无 status 键 → None（回退语义归 handler）"
    );

    // tool_calls / metadata / reasoning_content 原样提取（读时重建）
    let rich = rec(&json!({
        "role": "assistant",
        "content": "text",
        "tool_calls": [{"id": "c1"}],
        "reasoning_content": "thinking",
        "metadata": {"client_message_id": "cm-1"}
    }));
    assert!(rich.tool_calls_json.unwrap().contains("\"c1\""));
    assert_eq!(rich.reasoning_content.as_deref(), Some("thinking"));
    assert_eq!(rich.metadata.unwrap()["client_message_id"], "cm-1");
}

/// pending 输入 request 单列的解析契约（ADR-2026-11-17）：合法 JSON 原样
/// 还原；腐败值返回 None（调用方按毒丸处置——list 跳过 / pop 删除后取下一条，
/// 防单条坏行永久阻塞队列）。
#[test]
fn pending_request_parse_fails_closed() {
    let raw = serde_json::json!({
        "thread_id": "th1",
        "user_id": "u1",
        "pipeline_id": "p1",
        "content": "hi",
        "client_message_id": "cm-1",
        "source": "http",
        "execution_context": {"k": 1},
        "state_overlay": null,
        "pipeline_config_id": null,
    });
    let parsed =
        SqliteStore::parse_pending_request(&raw.to_string(), "p1").expect("合法 request 应可解析");
    assert_eq!(parsed.source, PendingInputSource::Http);
    assert_eq!(parsed.execution_context.unwrap()["k"], 1);
    assert!(parsed.state_overlay.is_none());
    assert!(
        SqliteStore::parse_pending_request("{corrupt", "p1").is_none(),
        "腐败 request 解析失败（毒丸语义，不静默造默认值）"
    );
}

// ── 旧库补列（migrate_add_*）：幂等 + 只在缺列时 ALTER ──

/// 旧库补列（migrate_add_*）：补列后默认值生效、既有数据不丢；再跑一次幂等
/// （不重复 ALTER 报错）。runs 表退役后补列面仅存 traces/users。
#[test]
fn migrate_add_columns_is_idempotent_and_preserves_data() {
    // ① traces 缺 tenant_id：补列后默认 'default'（存量数据归默认租户）
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(
            "CREATE TABLE traces (trace_id TEXT PRIMARY KEY, run_id TEXT);             INSERT INTO traces (trace_id) VALUES ('t1');",
        )
        .unwrap();
    migrate_add_tenant_id(&conn).unwrap();
    migrate_add_tenant_id(&conn).unwrap(); // 二次幂等
    let trace_tenant: String = conn
        .query_row(
            "SELECT tenant_id FROM traces WHERE trace_id = 't1'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(trace_tenant, "default", "存量 trace 归默认租户");
    // 补列不损失既有列数据
    conn.execute("UPDATE traces SET run_id = 'r9' WHERE trace_id = 't1'", [])
        .unwrap();
    let run_id: String = conn
        .query_row("SELECT run_id FROM traces WHERE trace_id = 't1'", [], |r| {
            r.get(0)
        })
        .unwrap();
    assert_eq!(run_id, "r9", "补列不损失既有列数据");

    // ② users 缺 must_change_password：补列默认 0（存量用户不被迫改口令）
    let conn2 = Connection::open_in_memory().unwrap();
    conn2
            .execute_batch(
                "CREATE TABLE users (user_id TEXT PRIMARY KEY);                 INSERT INTO users VALUES ('u1');",
            )
            .unwrap();
    migrate_add_users_must_change_password(&conn2).unwrap();
    migrate_add_users_must_change_password(&conn2).unwrap();
    let must_change: i64 = conn2
        .query_row(
            "SELECT must_change_password FROM users WHERE user_id = 'u1'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(must_change, 0, "存量用户默认不改口令");

    // ③ 全新库（无 traces 表）：补列函数跳过，不得炸 init（新形状由 DDL 直接建）
    let conn3 = Connection::open_in_memory().unwrap();
    migrate_add_tenant_id(&conn3).unwrap();
    let absent: i64 = conn3
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'traces'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(absent, 0, "全新库上补列 = 无操作");
}

/// 列已存在的库：补列函数不得 ALTER（重复 ALTER 会报 duplicate column）。
#[test]
fn migrate_add_columns_skips_when_already_present() {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(&ddl_sql()).unwrap(); // 完整 DDL 已含全部新列
                                             // 任一函数若误 ALTER 都会因 duplicate column 报错
    migrate_add_users_must_change_password(&conn).unwrap();
    migrate_add_tenant_id(&conn).unwrap();
}

// ── with_conn：结果与错误的薄透传契约 ──

/// with_conn 把闭包结果原样返回（Ok/Err 两路），错误类型不被改写。
#[test]
fn with_conn_passes_through_results_and_errors() {
    let store = SqliteStore::open_memory().unwrap();
    let ok: i64 = store
        .with_conn(|c| {
            c.query_row("SELECT 42", [], |r| r.get(0))
                .map_err(|e| e.to_string())
        })
        .unwrap();
    assert_eq!(ok, 42);

    let err = store
        .with_conn::<i64, String>(|_| Err("injected".to_string()))
        .unwrap_err();
    assert_eq!(err, "injected", "闭包错误原样透传");
}

// ── save_checkpoint / load_latest_checkpoint 往返与损坏传播 ──

/// 同一 step 重放幂等（INSERT OR REPLACE）；最新 step 胜出；他租户查不到。
#[tokio::test]
async fn checkpoint_replace_idempotent_and_latest_wins() {
    let store = SqliteStore::open_memory().unwrap();
    store
        .save_checkpoint("p_ck", "t1", 1, &json!({"v": "first"}))
        .unwrap();
    store
        .save_checkpoint("p_ck", "t1", 1, &json!({"v": "replayed"}))
        .unwrap();
    store
        .save_checkpoint("p_ck", "t1", 2, &json!({"v": "second"}))
        .unwrap();

    let (step, state) = store
        .load_latest_checkpoint("p_ck", "t1")
        .unwrap()
        .expect("有档应出口");
    assert_eq!(step, 2, "取最大 step");
    assert_eq!(state["v"], "second");
    assert_eq!(state["ckpt_max_seq"], json!(-1), "无 messages 时水位 -1");

    assert!(
        store
            .load_latest_checkpoint("p_ck", "other_tenant")
            .unwrap()
            .is_none(),
        "租户隔离：他租户查不到"
    );
}

/// `_` 前缀引擎私有键不入档（ADR 2026-09-29-trace-mirror-dedup）：per-run
/// staging（_full_tool_results 等）是 DR 零消费死重；非 `_` 合法键保留。
#[tokio::test]
async fn checkpoint_strips_underscore_engine_private_keys() {
    let store = SqliteStore::open_memory().unwrap();
    store
        .save_checkpoint(
            "p_us",
            "t1",
            1,
            &json!({
                "_full_tool_results": [{"tool_name": "bash_execute", "data": "X".repeat(128)}],
                "_executed_tool_calls": [{"name": "bash_execute"}],
                "task.status": "running",
                "system_message": {"role": "system", "content": "keep"},
            }),
        )
        .unwrap();
    let (_, state) = store
        .load_latest_checkpoint("p_us", "t1")
        .unwrap()
        .expect("有档应出口");
    assert!(
        state.get("_full_tool_results").is_none(),
        "全文 staging 键不得入档"
    );
    assert!(
        state.get("_executed_tool_calls").is_none(),
        "执行记录 staging 键不得入档"
    );
    assert_eq!(state["task.status"], "running", "非 `_` 合法键保留");
    assert_eq!(state["system_message"]["role"], "system", "合法键值保真");
    assert_eq!(state["ckpt_max_seq"], json!(-1), "水位照常写入");
}

/// checkpoint 行损坏（非 JSON）：显式 Serialization 错误，不静默当无档
/// （静默会让冷恢复丢标量基线且无痕迹）。
#[tokio::test]
async fn checkpoint_corrupt_row_surfaces_serialization_error() {
    let store = SqliteStore::open_memory().unwrap();
    store
            .conn
            .lock()
            .execute(
                "INSERT INTO pipeline_checkpoints                  (checkpoint_id, pipeline_id, step_no, state_json, tenant_id, created_at)                  VALUES ('cp_bad', 'p_bad', 0, '{not-json', 't1', '2026-01-01T00:00:00Z')",
                [],
            )
            .unwrap();
    let err = store
        .load_latest_checkpoint("p_bad", "t1")
        .expect_err("损坏 checkpoint 应报错而非静默 None");
    assert!(
        matches!(err, StorageError::Serialization(_)),
        "应为 Serialization 变体，实际: {err:?}"
    );
}

/// list_state_pipeline_ids：pipeline_state ∪ checkpoints 两源合并去重，
/// thread_id 优先取映射表（缺失回退 pipeline_id），按键升序。
#[tokio::test]
async fn list_state_pipeline_ids_merges_two_sources_and_sorts() {
    let store = SqliteStore::open_memory().unwrap();
    // 源① state 表；源② checkpoint；交集管道只应出现一次
    store
        .upsert_state_field("p_state_only", "t1", "k", &json!(1))
        .unwrap();
    store
        .save_checkpoint("p_ck_only", "t1", 0, &json!({"v": 1}))
        .unwrap();
    store
        .upsert_state_field("p_both", "t1", "k", &json!(1))
        .unwrap();
    store
        .save_checkpoint("p_both", "t1", 0, &json!({"v": 1}))
        .unwrap();
    // 映射表登记 p_state_only；p_ck_only 无映射 → thread 回退 pipeline_id
    store
        .link_pipeline_session("p_state_only", "thread-mapped", "t1")
        .await
        .unwrap();

    let rows = store.list_state_pipeline_ids("t1").unwrap();
    assert_eq!(
        rows,
        vec![
            ("p_both".to_string(), "p_both".to_string()),
            ("p_ck_only".to_string(), "p_ck_only".to_string()),
            ("p_state_only".to_string(), "thread-mapped".to_string()),
        ],
        "两源合并去重 + 映射优先 + 字典序"
    );
    assert!(store.list_state_pipeline_ids("other").unwrap().is_empty());
}

// ── 重发状态回退读面：load_checkpoint_before_seq / replace_pipeline_state ──

/// 按 `ckpt_max_seq < seq_exclusive` 取 step_no 最大的 checkpoint：
/// 目标 user 消息所属轮次开始前的状态基线；无满足条件 → None。
#[tokio::test]
async fn load_checkpoint_before_seq_picks_latest_below_watermark() {
    let store = SqliteStore::open_memory().unwrap();
    // 三份快照的水位分别 = 1 / 5 / 9（save_checkpoint 从 messages 最高 seq 写水位）
    let state_with_seq = |max_seq: u64, tag: &str| {
        let messages: Vec<serde_json::Value> = (0..=max_seq)
            .map(|seq| json!({"seq": seq, "role": "user", "content": "m"}))
            .collect();
        json!({"v": tag, "messages": messages})
    };
    store
        .save_checkpoint("p_rb", "t1", 1, &state_with_seq(1, "a"))
        .unwrap();
    store
        .save_checkpoint("p_rb", "t1", 2, &state_with_seq(5, "b"))
        .unwrap();
    store
        .save_checkpoint("p_rb", "t1", 3, &state_with_seq(9, "c"))
        .unwrap();

    let (step, state) = store
        .load_checkpoint_before_seq("p_rb", "t1", 5)
        .expect("水位 1 < 5 应命中")
        .expect("有档应出口");
    assert_eq!(step, 1, "水位 5/9 不满足 < 5，取 step 1");
    assert_eq!(state["v"], "a");
    assert!(state.get("messages").is_none(), "读面剥离 messages");

    let (step, state) = store
        .load_checkpoint_before_seq("p_rb", "t1", 6)
        .unwrap()
        .expect("水位 5 < 6，应取 step 2");
    assert_eq!(step, 2);
    assert_eq!(state["v"], "b");

    assert!(
        store
            .load_checkpoint_before_seq("p_rb", "t1", 1)
            .unwrap()
            .is_none(),
        "水位 1 不满足 < 1 → 无基线"
    );
    assert!(
        store
            .load_checkpoint_before_seq("p_rb", "other_tenant", 99)
            .unwrap()
            .is_none(),
        "租户隔离"
    );
}

/// replace_pipeline_state 先清后写：基线缺席的键（目标 step 之后才产生的）
/// 不得残留；同租户内替换，他租户不受影响。
#[tokio::test]
async fn replace_pipeline_state_clears_stale_keys() {
    let store = SqliteStore::open_memory().unwrap();
    store
        .upsert_state_field("p_rp", "t1", "keep", &json!("old"))
        .unwrap();
    store
        .upsert_state_field("p_rp", "t1", "future_only", &json!("later"))
        .unwrap();
    store
        .upsert_state_field("p_other", "t1", "keep", &json!("untouched"))
        .unwrap();

    let mut fields = serde_json::Map::new();
    fields.insert("keep".to_string(), json!("rolled_back"));
    store.replace_pipeline_state("p_rp", "t1", &fields).unwrap();

    let state = store.load_pipeline_state("p_rp", "t1").unwrap();
    assert_eq!(state.get("keep"), Some(&json!("rolled_back")));
    assert!(
        !state.contains_key("future_only"),
        "目标 step 之后新增的键必须删除"
    );
    let other = store.load_pipeline_state("p_other", "t1").unwrap();
    assert_eq!(
        other.get("keep"),
        Some(&json!("untouched")),
        "他管道不受影响"
    );
}
