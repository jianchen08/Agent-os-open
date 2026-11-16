use super::*;

// ── dispatch_regenerate：截断 ops + rollback trace + messages_truncated 事件
//    + skip_user_append 重跑（批次 D 原语）──
// 真实 SqliteStore + 真实 SessionCoordinator（关键路径走真实依赖，非全 mock）。

#[tokio::test]
async fn regenerate_truncates_after_target_and_appends_rollback_trace() {
    // 回退到 seq 0：其后全部槽位（1,2）置空；rollback trace 落库（run_id 复用
    // 消息所在 run，审计可反查）；messages_truncated 事件直达前端连接。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    seed_three_slot_history(&store, "reg1").await;
    // 目标 id 用读回的真实 record_id（写表时无 _message_id 注入 = 内容指纹）
    let user_id = store
        .get_messages_by_pipeline("reg1", MessageQueryOpts::default())
        .await
        .unwrap()
        .into_iter()
        .find(|m| m.seq_in_branch == 0)
        .map(|m| m.message_id)
        .unwrap();

    // 完整接线：SessionCoordinator + 注册线程 + 捕获 sink + AppState
    let coordinator = Arc::new(agentos_session::SessionCoordinator::new());
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coordinator.register_thread("T1", "u1");
    coordinator.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let mut state = crate::routes::AppState::new();
    state.store = Some(store.clone());
    seed_default_user(&*store, "u1").await;
    state.session = Some(coordinator.clone());
    let dispatcher = EngineDispatcher::new(state);

    dispatcher
        .dispatch_regenerate("u1", "T1", "reg1", &user_id, None)
        .await
        .unwrap();

    // 注销连接走 id() 比对路径（ConnectionRegistry::unregister）——补齐
    // sink 身份方法覆盖（非核心断言，注册表清理语义由 session crate 保证）
    coordinator.registry().unregister(
        "u1",
        coordinator
            .registry()
            .sinks_of_user("u1")
            .first()
            .expect("连接应仍注册")
            .id(),
    );

    // 表侧截断：seq 0 保留，1/2 成洞
    let rows = sqlite
        .get_slot_messages_by_pipeline("reg1", "default", MessageQueryOpts::default())
        .unwrap();
    let seqs: Vec<u32> = rows.iter().map(|r| r.seq_in_branch).collect();
    assert_eq!(seqs, vec![0], "目标 user 之后的槽位全部截断");
    assert_eq!(rows[0].content_preview.as_deref(), Some("第一问"));

    // 事件直达：messages_truncated（与 new_message 同通道）
    let truncated: Vec<String> = {
        let emitted = frames.lock().unwrap();
        emitted
            .iter()
            .filter(|f| f.contains("\"messages_truncated\""))
            .cloned()
            .collect()
    };
    assert_eq!(truncated.len(), 1, "截断事件必须发出");
    let payload: serde_json::Value = serde_json::from_str(&truncated[0]).unwrap();
    assert_eq!(payload["type"], "messages_truncated");
    assert_eq!(payload["data"]["pipeline_id"], "reg1");
    assert_eq!(payload["data"]["truncate_before_seq"], 1);

    // rollback trace：patch_type=rollback，patch_data.run_id 复用消息所在 run
    // （审计反查可见）
    let traces = store
        .get_step_traces_by_thread("T1", "default")
        .await
        .unwrap();
    let rollbacks: Vec<&TraceEntry> = traces
        .iter()
        .filter(|t| t.patch_type == agentos_core::types::PatchType::Rollback)
        .collect();
    assert_eq!(rollbacks.len(), 1, "恰好一条 rollback 轨迹");
    assert_eq!(
        rollbacks[0].patch_data["run_id"], "run-reg1",
        "run_id 复用消息所在 run"
    );
    assert_eq!(rollbacks[0].plugin_id, "chat_regenerate");
    assert_eq!(rollbacks[0].patch_data["rollback_to_user_seq"], 0);
}

#[tokio::test]
async fn regenerate_rejects_when_target_message_has_no_run_id() {
    // 目标 user 消息无有效 run_id（历史数据场景）：审计不可见即拒绝截断，
    // 不合成占位 id；消息槽位保持原样。
    let store =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap()) as Arc<dyn StorageBackend>;
    store
        .apply_messages_ops_to_table(
            "reg3",
            "default",
            &[
                json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "第一问"}}),
                json!({"op": "set", "seq": 1, "msg": {"role": "assistant", "content": "旧回复"}}),
            ],
        )
        .await
        .unwrap();
    store
        .record_run_start(
            "reg3",
            "default",
            "reg3",
            "cfg",
            "2026-10-15T00:00:00+00:00",
        )
        .await
        .unwrap();
    store
        .create_session(&session_record(Some("reg3")))
        .await
        .unwrap();
    store
        .link_pipeline_session("reg3", "T1", "default")
        .await
        .unwrap();

    let mut state = crate::routes::AppState::new();
    state.store = Some(store.clone());
    seed_default_user(&*store, "u1").await;
    let dispatcher = EngineDispatcher::new(state);
    let err = dispatcher
        .dispatch_regenerate("u1", "T1", "reg3", "", None)
        .await
        .unwrap_err();
    assert!(
        err.contains("无有效 run_id"),
        "拒绝原因应说明审计不可见: {err}"
    );

    // 截断未发生：两条槽位原样保留。
    let rows = store
        .get_messages_by_pipeline("reg3", MessageQueryOpts::default())
        .await
        .unwrap();
    assert_eq!(rows.len(), 2, "无 run_id 时不得截断");
}

#[tokio::test]
async fn regenerate_default_target_is_last_user_message() {
    // 缺省 user_message_id：定位最后一条 user（seq 2 场景：user/assistant/user）
    let store =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap()) as Arc<dyn StorageBackend>;
    store
            .apply_messages_ops_to_table(
                "reg2",
                "default",
                &[
                    json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "第一问"}, "_run_id": "run-reg2"}),
                    json!({"op": "set", "seq": 1, "msg": {"role": "assistant", "content": "旧回复"}, "_run_id": "run-reg2"}),
                    json!({"op": "set", "seq": 2, "msg": {"role": "user", "content": "第二问"}, "_run_id": "run-reg2"}),
                    json!({"op": "set", "seq": 3, "msg": {"role": "assistant", "content": "旧回复2"}, "_run_id": "run-reg2"}),
                ],
            )
            .await
            .unwrap();
    store
        .record_run_start(
            "reg2",
            "default",
            "reg2",
            "cfg",
            "2026-10-15T00:00:00+00:00",
        )
        .await
        .unwrap();
    store
        .create_session(&session_record(Some("reg2")))
        .await
        .unwrap();
    store
        .link_pipeline_session("reg2", "T1", "default")
        .await
        .unwrap();

    let mut state = crate::routes::AppState::new();
    state.store = Some(store.clone());
    seed_default_user(&*store, "u1").await;
    let dispatcher = EngineDispatcher::new(state);
    dispatcher
        .dispatch_regenerate("u1", "T1", "", "", None)
        .await
        .unwrap();

    let rows = store
        .get_messages_by_pipeline("reg2", MessageQueryOpts::default())
        .await
        .unwrap();
    let seqs: Vec<u32> = rows.iter().map(|r| r.seq_in_branch).collect();
    assert_eq!(seqs, vec![0, 1, 2], "缺省=最后一条 user（seq 2），其后截断");
    assert_eq!(rows[2].role, "user");
    assert_eq!(rows[2].content_preview.as_deref(), Some("第二问"));
}

#[tokio::test]
async fn regenerate_edit_resend_rewrites_content() {
    // 编辑重发：目标 user 消息内容改写（metadata 保留），其后截断
    let store =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap()) as Arc<dyn StorageBackend>;
    store
            .apply_messages_ops_to_table(
                "reg4",
                "default",
                &[
                    json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "旧问题",
                                                           "metadata": {"client_message_id": "cmid-1"}}, "_run_id": "run-reg4"}),
                    json!({"op": "set", "seq": 1, "msg": {"role": "assistant", "content": "旧回复"}, "_run_id": "run-reg4"}),
                ],
            )
            .await
            .unwrap();
    store
        .record_run_start(
            "reg4",
            "default",
            "reg4",
            "cfg",
            "2026-10-15T00:00:00+00:00",
        )
        .await
        .unwrap();
    store
        .create_session(&session_record(Some("reg4")))
        .await
        .unwrap();
    store
        .link_pipeline_session("reg4", "T1", "default")
        .await
        .unwrap();

    let mut state = crate::routes::AppState::new();
    state.store = Some(store.clone());
    seed_default_user(&*store, "u1").await;
    let dispatcher = EngineDispatcher::new(state);
    dispatcher
        .dispatch_regenerate("u1", "T1", "", "", Some("改写后的问题"))
        .await
        .unwrap();

    let rows = store
        .get_messages_by_pipeline("reg4", MessageQueryOpts::default())
        .await
        .unwrap();
    assert_eq!(rows.len(), 1, "assistant 槽位被截断");
    assert_eq!(rows[0].content_preview.as_deref(), Some("改写后的问题"));
    assert_eq!(
        rows[0]
            .metadata
            .as_ref()
            .and_then(|m| m.get("client_message_id"))
            .and_then(|v| v.as_str()),
        Some("cmid-1"),
        "改写保留原 metadata（cmid 幂等键）"
    );
}

/// 预置「压缩改写」场景（BUG-57 真实形状，装机版 2026-09-22 取证）：
/// seq 0 user 原文落库（带 run 锚）后，prepare 步压缩链路以改写内容重写
/// 同一槽位（无 _run_id → record_id 变异为新指纹）；user_input 轨迹账本
/// 保留原始 append 的（seq, 指纹, blob 锚）。返回（认领指纹, 改写后指纹）。
async fn seed_compressed_history(
    store: &Arc<dyn StorageBackend>,
    pipeline_id: &str,
    with_ledger: bool,
) -> (String, String) {
    let original = json!({"role": "user", "content": "第一问",
                              "metadata": {"client_message_id": "cmid-1"}});
    let rewritten = json!({"role": "user", "content": "<compressed>第一问</compressed>",
                               "metadata": {"client_message_id": "cmid-1"}});
    store
        .apply_messages_ops_to_table(
            pipeline_id,
            "default",
            &[
                json!({"op": "set", "seq": 0, "msg": original, "_run_id": "run-comp"}),
                json!({"op": "set", "seq": 1, "msg": {"role": "assistant", "content": "旧回复"},
                           "_run_id": "run-comp"}),
            ],
        )
        .await
        .unwrap();
    store
        .apply_messages_ops_to_table(
            pipeline_id,
            "default",
            &[json!({"op": "set", "seq": 0, "msg": rewritten})],
        )
        .await
        .unwrap();
    let claimed = agentos_core::ids::compute_message_id(&original);
    let rewritten_id = agentos_core::ids::compute_message_id(&rewritten);
    if with_ledger {
        let bytes = serde_json::to_string(&original).unwrap();
        let blob_id = store
            .store_blob(bytes.as_bytes(), "application/json")
            .await
            .unwrap();
        store
            .append_trace(TraceEntry {
                trace_id: format!("t_ledger_{pipeline_id}"),
                pipeline_id: pipeline_id.to_string(),
                seq: 0,
                plugin_id: "user_input".to_string(),
                patch_type: agentos_core::types::PatchType::StateUpdate,
                patch_data: json!({"messages": {"_ops": [
                    {"op": "set", "seq": 0, "message_id": claimed, "blob_id": blob_id},
                ]}}),
                created_at: "2026-09-22T12:57:12Z".to_string(),
            })
            .await
            .unwrap();
    }
    store
        .record_run_start(
            pipeline_id,
            "default",
            "run-comp",
            "cfg",
            "2026-10-15T00:00:00+00:00",
        )
        .await
        .unwrap();
    store
        .create_session(&session_record(Some(pipeline_id)))
        .await
        .unwrap();
    store
        .link_pipeline_session(pipeline_id, "T1", "default")
        .await
        .unwrap();
    (claimed, rewritten_id)
}

#[tokio::test]
async fn regenerate_resolves_claimed_id_after_compression_rewrite() {
    // BUG-57 回归钉：压缩链路重写目标槽位后 record_id 变异，前端仍持认领
    // 指纹回退——槽位查找 miss 时经 trace 账本回溯定位并恢复原文，截断其后。
    let store =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap()) as Arc<dyn StorageBackend>;
    let (claimed, rewritten_id) = seed_compressed_history(&store, "regA", true).await;
    assert_ne!(claimed, rewritten_id, "改写后指纹必须变异（场景前提）");

    let mut state = crate::routes::AppState::new();
    state.store = Some(store.clone());
    seed_default_user(&*store, "u1").await;
    let dispatcher = EngineDispatcher::new(state);
    dispatcher
        .dispatch_regenerate("u1", "T1", "regA", &claimed, None)
        .await
        .expect("账本回溯应使命令的 record id 仍可回退");

    let rows = store
        .get_messages_by_pipeline("regA", MessageQueryOpts::default())
        .await
        .unwrap();
    let seqs: Vec<u32> = rows.iter().map(|r| r.seq_in_branch).collect();
    assert_eq!(seqs, vec![0], "目标之后的槽位全部截断");
    assert_eq!(rows[0].message_id, claimed, "原文恢复 → 指纹回归认领 id");
    assert_eq!(
        rows[0].content_preview.as_deref(),
        Some("第一问"),
        "改写内容被原文恢复（不是压缩变体）"
    );

    // rollback trace：run 锚不可恢复时以账本锚替代（可反查）
    let traces = store
        .get_step_traces_by_thread("T1", "default")
        .await
        .unwrap();
    let rollback = traces
        .iter()
        .find(|t| t.patch_type == agentos_core::types::PatchType::Rollback)
        .expect("恰好一条 rollback 轨迹");
    assert_eq!(rollback.patch_data["resolution"], "trace_ledger");
    assert_eq!(rollback.patch_data["ledger_trace_id"], "t_ledger_regA");
    assert_eq!(rollback.patch_data["rollback_to_user_seq"], 0);
}

#[tokio::test]
async fn regenerate_edit_resend_on_compressed_target_restores_with_new_content() {
    // 编辑重发命中压缩改写槽位：以账本原文的 metadata（cmid 幂等键）+
    // 新内容恢复目标槽位，其后截断。
    let store =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap()) as Arc<dyn StorageBackend>;
    let (claimed, _) = seed_compressed_history(&store, "regB", true).await;

    let mut state = crate::routes::AppState::new();
    state.store = Some(store.clone());
    seed_default_user(&*store, "u1").await;
    let dispatcher = EngineDispatcher::new(state);
    dispatcher
        .dispatch_regenerate("u1", "T1", "regB", &claimed, Some("改写后的问题"))
        .await
        .unwrap();

    let rows = store
        .get_messages_by_pipeline("regB", MessageQueryOpts::default())
        .await
        .unwrap();
    let seqs: Vec<u32> = rows.iter().map(|r| r.seq_in_branch).collect();
    assert_eq!(seqs, vec![0], "目标之后的槽位全部截断");
    assert_eq!(rows[0].content_preview.as_deref(), Some("改写后的问题"));
    assert_eq!(
        rows[0]
            .metadata
            .as_ref()
            .and_then(|m| m.get("client_message_id"))
            .and_then(|v| v.as_str()),
        Some("cmid-1"),
        "metadata 取自账本原文（cmid 幂等键不丢）"
    );
}

#[tokio::test]
async fn regenerate_unknown_id_without_ledger_still_rejects() {
    // 无账本锚的未知 id 维持 fail-closed：报错不截断（不静默清空、不降低
    // 审计门槛）。
    let store =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap()) as Arc<dyn StorageBackend>;
    let (claimed, _) = seed_compressed_history(&store, "regC", false).await;

    let mut state = crate::routes::AppState::new();
    state.store = Some(store.clone());
    seed_default_user(&*store, "u1").await;
    let dispatcher = EngineDispatcher::new(state);
    let err = dispatcher
        .dispatch_regenerate("u1", "T1", "regC", &claimed, None)
        .await
        .unwrap_err();
    assert!(
        err.contains("未找到目标 user 消息"),
        "无账本锚维持原拒绝语义: {err}"
    );
    let rows = store
        .get_messages_by_pipeline("regC", MessageQueryOpts::default())
        .await
        .unwrap();
    assert_eq!(rows.len(), 2, "拒绝时槽位原样保留");
}

#[tokio::test]
async fn regenerate_ledger_rejects_non_user_fingerprint() {
    // 账本锚只认 role=user：assistant 指纹不可作截断边界（工具配对完整性），
    // 即便账本实录与 blob 均在场也拒绝。
    let store =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap()) as Arc<dyn StorageBackend>;
    let assistant = json!({"role": "assistant", "content": "回答"});
    let assistant_id = agentos_core::ids::compute_message_id(&assistant);
    seed_compressed_history(&store, "regD", false).await;
    let bytes = serde_json::to_string(&assistant).unwrap();
    let blob_id = store
        .store_blob(bytes.as_bytes(), "application/json")
        .await
        .unwrap();
    store
        .append_trace(TraceEntry {
            trace_id: "t_ledger_regD_a".to_string(),
            pipeline_id: "regD".to_string(),
            seq: 1,
            plugin_id: "user_input".to_string(),
            patch_type: agentos_core::types::PatchType::StateUpdate,
            patch_data: json!({"messages": {"_ops": [
                {"op": "set", "seq": 1, "message_id": assistant_id, "blob_id": blob_id},
            ]}}),
            created_at: "2026-09-22T12:57:13Z".to_string(),
        })
        .await
        .unwrap();

    let mut state = crate::routes::AppState::new();
    state.store = Some(store.clone());
    seed_default_user(&*store, "u1").await;
    let dispatcher = EngineDispatcher::new(state);
    let err = dispatcher
        .dispatch_regenerate("u1", "T1", "regD", &assistant_id, None)
        .await
        .unwrap_err();
    assert!(
        err.contains("未找到目标 user 消息"),
        "非 user 指纹不可作截断边界: {err}"
    );
    let rows = store
        .get_messages_by_pipeline("regD", MessageQueryOpts::default())
        .await
        .unwrap();
    assert_eq!(rows.len(), 2, "拒绝时槽位原样保留");
}

#[tokio::test]
async fn regenerate_resolves_target_by_client_message_id_when_claim_missing() {
    // 认领缺失窗口（失败 run 不发认领载荷 → 前端消息无 recordId）：出站退回
    // UI uuid = metadata.client_message_id（随 blob 持久化的幂等键）。目标解析
    // 按 cmid 等值命中，截断重跑照常（装机 2026-10-16 实证：LLM 401 失败轮后
    // 回退/编辑重发全被「未找到目标 user 消息」拒绝且前端静默）。
    let store =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap()) as Arc<dyn StorageBackend>;
    store
        .apply_messages_ops_to_table(
            "regE",
            "default",
            &[
                json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "第一问",
                               "metadata": {"client_message_id": "686a7736-7f46-44a0-8827-b9f05603063b"}},
                           "_run_id": "run-regE"}),
                json!({"op": "set", "seq": 1, "msg": {"role": "assistant", "content": "旧回复"},
                           "_run_id": "run-regE"}),
            ],
        )
        .await
        .unwrap();
    store
        .record_run_start(
            "regE",
            "default",
            "run-regE",
            "cfg",
            "2026-10-16T00:00:00+00:00",
        )
        .await
        .unwrap();
    store
        .create_session(&session_record(Some("regE")))
        .await
        .unwrap();
    store
        .link_pipeline_session("regE", "T1", "default")
        .await
        .unwrap();

    let mut state = crate::routes::AppState::new();
    state.store = Some(store.clone());
    seed_default_user(&*store, "u1").await;
    let dispatcher = EngineDispatcher::new(state);
    // 出站 id = 前端 UI uuid（cmid），不是 mc_ 指纹
    dispatcher
        .dispatch_regenerate(
            "u1",
            "T1",
            "regE",
            "686a7736-7f46-44a0-8827-b9f05603063b",
            None,
        )
        .await
        .expect("cmid 兜底应使命令的 UI uuid 可回退");

    let rows = store
        .get_messages_by_pipeline("regE", MessageQueryOpts::default())
        .await
        .unwrap();
    let seqs: Vec<u32> = rows.iter().map(|r| r.seq_in_branch).collect();
    assert_eq!(seqs, vec![0], "cmid 命中目标，其后槽位截断");
    assert_eq!(rows[0].role, "user");

    let traces = store
        .get_step_traces_by_thread("T1", "default")
        .await
        .unwrap();
    let rollback = traces
        .iter()
        .find(|t| t.patch_type == agentos_core::types::PatchType::Rollback)
        .expect("rollback 轨迹应落库");
    assert_eq!(
        rollback.patch_data["user_message_id"],
        "686a7736-7f46-44a0-8827-b9f05603063b"
    );
    assert_eq!(
        rollback.patch_data["run_id"], "run-regE",
        "审计锚 = 槽位 run_id（Live 路径）"
    );
}

#[tokio::test]
async fn regenerate_cmid_match_is_user_only_and_id_match_wins() {
    // cmid 兜底只认 role=user（assistant 槽位即使 metadata 携带同值也不可作
    // 截断边界）；record_id 精确匹配优先于 cmid 匹配。
    let mk =
        |id: &str, role: &str, seq: u32, cmid: Option<&str>| agentos_core::types::MessageRecord {
            message_id: id.to_string(),
            run_id: "r".to_string(),
            seq_in_branch: seq,
            role: role.to_string(),
            blob_id: None,
            content_preview: None,
            created_at: "".to_string(),
            pipeline_id: None,
            tool_calls_json: None,
            tool_call_id: None,
            reasoning_content: None,
            status: None,
            error: None,
            tool_result_json: None,
            metadata: cmid.map(|c| json!({"client_message_id": c})),
            agent_id: None,
        };
    let msgs = vec![
        mk("mc_aaa", "user", 0, Some("cmid-1")),
        mk("mc_bbb", "assistant", 1, Some("cmid-1")),
        mk("mc_ccc", "user", 2, None),
    ];
    assert_eq!(
        find_target_user_seq(&msgs, "mc_aaa"),
        Some(0),
        "record_id 精确匹配优先"
    );
    assert_eq!(
        find_target_user_seq(&msgs, "cmid-1"),
        Some(0),
        "cmid 兜底命中 user 槽位"
    );
    let assistant_only = vec![mk("mc_bbb", "assistant", 1, Some("cmid-2"))];
    assert_eq!(
        find_target_user_seq(&assistant_only, "cmid-2"),
        None,
        "assistant 槽位不可经 cmid 作截断边界"
    );
}

#[tokio::test]
async fn regenerate_unknown_target_returns_error() {
    // 目标 user 消息不存在 → 报错不截断（不静默清空）
    let store =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap()) as Arc<dyn StorageBackend>;
    store
        .apply_messages_ops_to_table(
            "reg5",
            "default",
            &[json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "第一问"}})],
        )
        .await
        .unwrap();
    store
        .record_run_start(
            "reg5",
            "default",
            "reg5",
            "cfg",
            "2026-10-15T00:00:00+00:00",
        )
        .await
        .unwrap();
    store
        .create_session(&session_record(Some("reg5")))
        .await
        .unwrap();
    store
        .link_pipeline_session("reg5", "T1", "default")
        .await
        .unwrap();

    let mut state = crate::routes::AppState::new();
    state.store = Some(store.clone());
    seed_default_user(&*store, "u1").await;
    let dispatcher = EngineDispatcher::new(state);
    let err = dispatcher
        .dispatch_regenerate("u1", "T1", "reg5", "no_such_id", None)
        .await
        .unwrap_err();
    assert!(err.contains("未找到目标 user 消息"), "err: {err}");

    // 槽位原样（未被误截断）
    let rows = store
        .get_messages_by_pipeline("reg5", MessageQueryOpts::default())
        .await
        .unwrap();
    assert_eq!(rows.len(), 1);
}

#[tokio::test]
async fn find_target_user_seq_prefers_id_else_last_user() {
    // 定位原语：id 精确匹配（只接受 role=user）vs 缺省最后一条 user
    let mk = |id: &str, role: &str, seq: u32| agentos_core::types::MessageRecord {
        message_id: id.to_string(),
        run_id: "r".to_string(),
        seq_in_branch: seq,
        role: role.to_string(),
        blob_id: None,
        content_preview: None,
        created_at: "".to_string(),
        pipeline_id: None,
        tool_calls_json: None,
        tool_call_id: None,
        reasoning_content: None,
        status: None,
        error: None,
        tool_result_json: None,
        metadata: None,
        agent_id: None,
    };
    let msgs = vec![
        mk("m0", "user", 0),
        mk("a1", "assistant", 1),
        mk("m2", "user", 2),
        mk("a3", "assistant", 3),
    ];
    assert_eq!(
        find_target_user_seq(&msgs, "m0"),
        Some(0),
        "显式 id 定位早期 user"
    );
    assert_eq!(
        find_target_user_seq(&msgs, ""),
        Some(2),
        "缺省=最后一条 user（跳过尾部 assistant）"
    );
    assert_eq!(
        find_target_user_seq(&msgs, "a1"),
        None,
        "assistant id 不是合法截断边界"
    );
    assert_eq!(find_target_user_seq(&msgs, "ghost"), None);
}

#[tokio::test]
async fn regenerate_rejected_while_chain_busy() {
    // 运行态守卫（聊天消息流权威化设计真值 20261027 不变量 3）：链忙（run 在
    // 飞/收尾在途）时 regenerate 拒绝——在飞 run 仍向被截后缀 append，截断落库
    // 与其竞争必出分叉（2026-10-27 装机实证：在飞回退 → 空 assistant 槽位 +
    // 重跑滞留队列被停止清队吞掉）。拒绝后释放链，同一请求放行（守卫是唯一
    // 拦截面）。
    let store =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap()) as Arc<dyn StorageBackend>;
    store
        .apply_messages_ops_to_table(
            "regBusy",
            "default",
            &[
                json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "第一问"}, "_run_id": "run-regBusy"}),
                json!({"op": "set", "seq": 1, "msg": {"role": "assistant", "content": "旧回复"}, "_run_id": "run-regBusy"}),
            ],
        )
        .await
        .unwrap();
    store
        .record_run_start(
            "regBusy",
            "default",
            "run-regBusy",
            "cfg",
            "2026-10-15T00:00:00+00:00",
        )
        .await
        .unwrap();
    store
        .create_session(&session_record(Some("regBusy")))
        .await
        .unwrap();
    store
        .link_pipeline_session("regBusy", "T1", "default")
        .await
        .unwrap();

    let mut state = crate::routes::AppState::new();
    state.store = Some(store.clone());
    seed_default_user(&*store, "u1").await;
    let dispatcher = EngineDispatcher::new(state);

    // 占链（fut 挂起等释放）模拟在飞 run；管道 id 带测试专名防污染进程级注册表。
    let registry = crate::run_chain::RunChainRegistry::global();
    let (release_tx, release_rx) = tokio::sync::oneshot::channel::<()>();
    registry.try_enqueue_direct("regBusy", "u1", async move {
        let _ = release_rx.await;
    });
    assert!(registry.has_chain("regBusy"), "占链前置条件");

    let err = dispatcher
        .dispatch_regenerate("u1", "T1", "regBusy", "", None)
        .await
        .unwrap_err();
    assert!(
        err.contains("请先停止生成"),
        "拒绝文案必须给用户指引: {err}"
    );
    let rows = store
        .get_messages_by_pipeline("regBusy", MessageQueryOpts::default())
        .await
        .unwrap();
    assert_eq!(rows.len(), 2, "链忙拒绝时槽位原样保留");

    // 释放链：守卫解除，同一请求照常截断重跑。
    release_tx.send(()).unwrap();
    // 占链任务的自清理是独立 spawned 任务——须让出调度权待它出链（满批
    // 负载下立即重派曾见 stale busy 假红：清理尚未被调度，忙检仍在册）。
    // 有界等待（200×10ms），不依赖调度运气。
    let mut released = false;
    for _ in 0..200 {
        if !registry.has_chain("regBusy") {
            released = true;
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
    }
    assert!(released, "释放后链必须出册（2s 有界窗）");
    dispatcher
        .dispatch_regenerate("u1", "T1", "regBusy", "", None)
        .await
        .expect("链空闲后 regenerate 放行");
    let rows = store
        .get_messages_by_pipeline("regBusy", MessageQueryOpts::default())
        .await
        .unwrap();
    assert_eq!(rows.len(), 1, "放行后目标之后的槽位截断");
}
