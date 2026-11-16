use super::*;

use super::support::{seed_three_slot_history, CapturingSink};

// ── 消息段：regenerate 冻结 + segment_activate（多代切换）──

/// regenerate 截断前把被换后缀冻结为段（同批 ops 同事务）：段统一含目标
/// user 消息（[target_seq..末尾]，edit-resend 改写目标槽位后旧用户文本仍在
/// 段里），成员为槽位全文，rollback trace 的 messages._ops 携带 freeze op 全文。
#[tokio::test]
async fn regenerate_freezes_deleted_suffix_segment() {
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    seed_three_slot_history(&store, "regSeg1").await;
    let user_id = store
        .get_messages_by_pipeline("regSeg1", MessageQueryOpts::default())
        .await
        .unwrap()
        .into_iter()
        .find(|m| m.seq_in_branch == 0)
        .map(|m| m.message_id)
        .unwrap();
    let mut state = crate::routes::AppState::new();
    state.store = Some(store.clone());
    seed_default_user(&*store, "u1").await;
    let dispatcher = EngineDispatcher::new(state);

    dispatcher
        .dispatch_regenerate("u1", "T1", "regSeg1", &user_id, None)
        .await
        .unwrap();

    // 被换后缀（seq 0..2 整段，含目标 user）冻结为段：visible_to=''、
    // preview 取首条、成员全文
    let segments = store
        .list_message_segments("regSeg1", "default")
        .await
        .unwrap();
    assert_eq!(segments.len(), 1, "被换后缀冻结为一段");
    let seg = &segments[0];
    assert_eq!((seg.base_seq, seg.base_len), (0, 3));
    assert_eq!(seg.visible_to, "");
    assert_eq!(seg.preview.as_deref(), Some("第一问"));
    let full = store
        .get_message_segment(&seg.id, "default")
        .await
        .unwrap()
        .expect("段可解析");
    let members = full.members.unwrap();
    assert_eq!(members[0]["role"], "user");
    assert_eq!(members[0]["content"], "第一问", "旧用户消息原文入段");
    assert!(
        members[1].get("tool_calls").is_some(),
        "成员为槽位全文（含 tool_calls，非截断 preview）"
    );
    assert_eq!(members[2]["role"], "tool");
    assert!(
        members.iter().all(|m| m.get("seq").is_none()),
        "冻结成员不带槽位位置标记（seq 写回时变陈旧残留）"
    );

    // rollback trace 的 messages._ops 首位即 freeze op 全文（id 与段行一致）
    let traces = store
        .get_step_traces_by_thread("T1", "default")
        .await
        .unwrap();
    let rollback = traces
        .iter()
        .find(|t| t.patch_type == agentos_core::types::PatchType::Rollback)
        .expect("rollback 轨迹在场");
    let ops = rollback.patch_data["messages"]["_ops"]
        .as_array()
        .expect("_ops 数组");
    assert_eq!(ops[0]["op"], "freeze_segment");
    assert_eq!(ops[0]["id"], seg.id);
    assert_eq!(ops[0]["base_seq"], 0);
}

/// 统一含 target_seq 的动因回归：edit-resend 原地改写目标槽位后，旧用户
/// 文本仍在段里——激活该段连用户消息一起恢复（否则新用户文本配旧轮次）。
#[tokio::test]
async fn regenerate_edit_resend_old_user_text_restorable_via_segment() {
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    seed_three_slot_history(&store, "regSeg2").await;
    let user_id = store
        .get_messages_by_pipeline("regSeg2", MessageQueryOpts::default())
        .await
        .unwrap()
        .into_iter()
        .find(|m| m.seq_in_branch == 0)
        .map(|m| m.message_id)
        .unwrap();
    let mut state = crate::routes::AppState::new();
    state.store = Some(store.clone());
    seed_default_user(&*store, "u1").await;
    let dispatcher = EngineDispatcher::new(state);
    dispatcher
        .dispatch_regenerate("u1", "T1", "regSeg2", &user_id, Some("改写后的问题"))
        .await
        .unwrap();

    // 目标槽位已原地改写，旧文本只在冻结段里
    let rows = store
        .get_messages_by_pipeline("regSeg2", MessageQueryOpts::default())
        .await
        .unwrap();
    assert_eq!(rows[0].content_preview.as_deref(), Some("改写后的问题"));
    let seg = store
        .list_message_segments("regSeg2", "default")
        .await
        .unwrap()
        .into_iter()
        .next()
        .expect("edit-resend 也冻结段");
    assert_eq!((seg.base_seq, seg.base_len), (0, 3));
    let old_user_text = store
        .get_message_segment(&seg.id, "default")
        .await
        .unwrap()
        .unwrap()
        .members
        .unwrap()[0]["content"]
        .as_str()
        .unwrap()
        .to_string();
    assert_eq!(old_user_text, "第一问", "旧用户文本经段可达");

    // run 收尾（激活运行态守卫），切回旧代：用户消息连旧轮次一起恢复
    store
        .upsert_state_fields(
            "regSeg2",
            "default",
            &json!({ "run_status": "completed" })
                .as_object()
                .unwrap()
                .clone(),
        )
        .await
        .unwrap();
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    let activator = segment_dispatcher(&store, &frames);
    activator
        .dispatch_segment_activate("u1", "T1", "regSeg2", &seg.id)
        .await
        .unwrap();

    let rows = sqlite
        .get_slot_messages_by_pipeline("regSeg2", "default", MessageQueryOpts::default())
        .unwrap();
    let got: Vec<(u32, String)> = rows
        .iter()
        .map(|r| {
            (
                r.seq_in_branch,
                r.content_preview.clone().unwrap_or_default(),
            )
        })
        .collect();
    assert_eq!(
        got,
        vec![
            (0, "第一问".into()),
            (1, "旧回复".into()),
            (2, "{\"success\":true,\"data\":\"ok\"}".into())
        ],
        "激活恢复旧代：旧用户文本 + 旧轮次"
    );
}

/// 预置激活场景：u@0 + gen1 代（A/B，已冻结为段 seg_gen1）+ 当前代
/// （新A/新B/新C，比 gen1 长）。run 簿记不落（无 running run，激活不设防）。
async fn seed_activate_history(store: &Arc<dyn StorageBackend>, pid: &str) {
    let a = json!({"role": "assistant", "content": "旧A"});
    let b = json!({"role": "assistant", "content": "旧B"});
    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[
                json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "问"}}),
                json!({"op": "set", "seq": 1, "msg": a}),
                json!({"op": "set", "seq": 2, "msg": b}),
                json!({
                    "op": "freeze_segment", "id": "seg_gen1",
                    "base_seq": 1, "base_len": 2, "visible_to": "",
                    "preview": "旧A", "members": [a, b],
                }),
            ],
        )
        .await
        .unwrap();
    // 当前代（3 条 > gen1 的 2 条）
    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[
                json!({"op": "set", "seq": 1, "msg": {"role": "assistant", "content": "新A"}}),
                json!({"op": "set", "seq": 2, "msg": {"role": "assistant", "content": "新B"}}),
                json!({"op": "set", "seq": 3, "msg": {"role": "assistant", "content": "新C"}}),
            ],
        )
        .await
        .unwrap();
    store
        .create_session(&session_record(Some(pid)))
        .await
        .unwrap();
    store
        .link_pipeline_session(pid, "T1", "default")
        .await
        .unwrap();
}

/// 接线 dispatcher + 捕获 sink（segment_activate 测试共用：ack 事件直达断言）。
fn segment_dispatcher(
    store: &Arc<dyn StorageBackend>,
    frames: &Arc<Mutex<Vec<String>>>,
) -> EngineDispatcher {
    let coordinator = Arc::new(agentos_session::SessionCoordinator::new());
    coordinator.register_thread("T1", "u1");
    coordinator.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let mut state = crate::routes::AppState::new();
    state.store = Some(store.clone());
    state.session = Some(coordinator);
    EngineDispatcher::new(state)
}

fn frames_of_type(frames: &[String], event_type: &str) -> Vec<serde_json::Value> {
    frames
        .iter()
        .filter(|f| f.contains(&format!("\"{event_type}\"")))
        .map(|f| serde_json::from_str(f).unwrap())
        .collect()
}

#[tokio::test]
async fn segment_activate_freezes_suffix_writes_back_and_clears_extra_slots() {
    // 后缀整段替换（§2.5）：当前 [base_seq..末尾]（3 条 > 目标 2 条）先冻结
    // 为新段（无损，切换不变量），目标段成员逐槽写回，多余槽 set null。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    seed_activate_history(&store, "act1").await;
    seed_default_user(&*store, "u1").await;
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    let dispatcher = segment_dispatcher(&store, &frames);

    dispatcher
        .dispatch_segment_activate("u1", "T1", "act1", "seg_gen1")
        .await
        .unwrap();

    // 启用序列写回：seq0 user 不动，seq1/2 回到 gen1，seq3（多余槽）清空
    let rows = sqlite
        .get_slot_messages_by_pipeline("act1", "default", MessageQueryOpts::default())
        .unwrap();
    let got: Vec<(u32, String)> = rows
        .iter()
        .map(|r| {
            (
                r.seq_in_branch,
                r.content_preview.clone().unwrap_or_default(),
            )
        })
        .collect();
    assert_eq!(
        got,
        vec![(0, "问".into()), (1, "旧A".into()), (2, "旧B".into())],
        "目标段写回 + 多余槽清空"
    );

    // 当前后缀已先冻结为新段（旧代无损可切回）
    let segments = store
        .list_message_segments("act1", "default")
        .await
        .unwrap();
    assert_eq!(segments.len(), 2, "gen1 段 + 新冻结后缀段");
    let frozen = segments.iter().find(|s| s.id != "seg_gen1").expect("新段");
    assert_eq!((frozen.base_seq, frozen.base_len), (1, 3));
    assert_eq!(frozen.preview.as_deref(), Some("新A"), "preview 取首条");
    let frozen_full = store
        .get_message_segment(&frozen.id, "default")
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        frozen_full.members.unwrap()[2]["content"],
        "新C",
        "被换下后缀完整入段（切换无损）"
    );

    // trace 落痕：目标段行 + 全部 ops（含 freeze op）
    let traces = store
        .get_step_traces_by_thread("T1", "default")
        .await
        .unwrap();
    let tr = traces
        .iter()
        .find(|t| t.patch_data.get("action") == Some(&json!("segment_activate")))
        .expect("激活 trace 在场");
    assert_eq!(tr.patch_data["activated_segment"]["id"], "seg_gen1");
    let ops = tr.patch_data["messages"]["_ops"].as_array().unwrap();
    assert_eq!(ops[0]["op"], "freeze_segment", "冻结先于覆写");

    // ack 事件（多端对账）
    let acks = frames_of_type(&frames.lock().unwrap(), "segment_activated");
    assert_eq!(acks.len(), 1, "ack 恰好一次");
    assert_eq!(acks[0]["data"]["pipeline_id"], "act1");
    assert_eq!(acks[0]["data"]["segment_id"], "seg_gen1");
}

#[tokio::test]
async fn segment_activate_target_longer_appends_new_slots() {
    // 目标段比当前后缀长：不足槽位新增（写回按目标段内容）
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let a = json!({"role": "assistant", "content": "A"});
    let b = json!({"role": "assistant", "content": "B"});
    let c = json!({"role": "assistant", "content": "C"});
    store
        .apply_messages_ops_to_table(
            "act2",
            "default",
            &[
                json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "问"}}),
                json!({"op": "set", "seq": 1, "msg": a}),
                json!({"op": "set", "seq": 2, "msg": b}),
                json!({"op": "set", "seq": 3, "msg": c}),
                json!({
                    "op": "freeze_segment", "id": "seg3",
                    "base_seq": 1, "base_len": 3, "visible_to": "",
                    "preview": "A", "members": [a, b, c],
                }),
                // 当前代截短到 1 条（目标段长于当前后缀）
                json!({"op": "set", "seq": 2, "msg": serde_json::Value::Null}),
                json!({"op": "set", "seq": 3, "msg": serde_json::Value::Null}),
            ],
        )
        .await
        .unwrap();
    store
        .create_session(&session_record(Some("act2")))
        .await
        .unwrap();
    store
        .link_pipeline_session("act2", "T1", "default")
        .await
        .unwrap();
    seed_default_user(&*store, "u1").await;
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    let dispatcher = segment_dispatcher(&store, &frames);

    dispatcher
        .dispatch_segment_activate("u1", "T1", "act2", "seg3")
        .await
        .unwrap();

    let rows = sqlite
        .get_slot_messages_by_pipeline("act2", "default", MessageQueryOpts::default())
        .unwrap();
    let got: Vec<(u32, String)> = rows
        .iter()
        .map(|r| {
            (
                r.seq_in_branch,
                r.content_preview.clone().unwrap_or_default(),
            )
        })
        .collect();
    assert_eq!(
        got,
        vec![
            (0, "问".into()),
            (1, "A".into()),
            (2, "B".into()),
            (3, "C".into())
        ],
        "目标更长 → 缺失槽位逐槽补齐"
    );
}

#[tokio::test]
async fn segment_activate_repeat_delivery_is_zero_change() {
    // 幂等（§7.3-2）：已激活态重复投递 → 零变化（不建新段、不落 trace、
    // 槽位不动），仅 ack 照发（多端对账）。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    seed_activate_history(&store, "act3").await;
    seed_default_user(&*store, "u1").await;
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    let dispatcher = segment_dispatcher(&store, &frames);

    dispatcher
        .dispatch_segment_activate("u1", "T1", "act3", "seg_gen1")
        .await
        .unwrap();
    let segments_before = store
        .list_message_segments("act3", "default")
        .await
        .unwrap();
    let slots_before = sqlite
        .get_slot_messages_by_pipeline("act3", "default", MessageQueryOpts::default())
        .unwrap();
    let traces_before = store
        .get_step_traces_by_thread("T1", "default")
        .await
        .unwrap();
    let acks_before = frames_of_type(&frames.lock().unwrap(), "segment_activated").len();

    dispatcher
        .dispatch_segment_activate("u1", "T1", "act3", "seg_gen1")
        .await
        .unwrap();

    let segments_after = store
        .list_message_segments("act3", "default")
        .await
        .unwrap();
    let slots_after = sqlite
        .get_slot_messages_by_pipeline("act3", "default", MessageQueryOpts::default())
        .unwrap();
    let traces_after = store
        .get_step_traces_by_thread("T1", "default")
        .await
        .unwrap();
    assert_eq!(
        serde_json::to_string(&segments_before).unwrap(),
        serde_json::to_string(&segments_after).unwrap(),
        "重复投递不建新段"
    );
    assert_eq!(
        serde_json::to_string(&slots_before).unwrap(),
        serde_json::to_string(&slots_after).unwrap(),
        "重复投递槽位零变化"
    );
    assert_eq!(
        traces_before.len(),
        traces_after.len(),
        "重复投递不追加 trace"
    );
    assert_eq!(
        acks_before + 1,
        frames_of_type(&frames.lock().unwrap(), "segment_activated").len(),
        "ack 每次投递照发（多端对账）"
    );
}

#[tokio::test]
async fn segment_activate_switch_back_to_archived_content_skips_duplicate_freeze() {
    // 冻结查重（分支重复增殖根修）：切到 gen1 时当前后缀（新代 3 条）冻结为
    // 新段；再切回该新段时当前后缀 = gen1 内容，与 seg_gen1 逐条同指纹——
    // 不再重复冻结（否则 A↔B 每切一次段表涨一行，‹i/n› 的 n 虚长），
    // 写回与 ack 照常。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    seed_activate_history(&store, "act6").await;
    seed_default_user(&*store, "u1").await;
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    let dispatcher = segment_dispatcher(&store, &frames);

    // 第一跳：切到 gen1（当前后缀 = 新代，首次被换下，合法冻结一段）
    dispatcher
        .dispatch_segment_activate("u1", "T1", "act6", "seg_gen1")
        .await
        .unwrap();
    let new_gen_seg = store
        .list_message_segments("act6", "default")
        .await
        .unwrap()
        .into_iter()
        .find(|s| s.id != "seg_gen1")
        .expect("第一跳冻结的新代段在场");

    // 第二跳：切回新代（当前后缀 = gen1 内容，与 seg_gen1 同指纹 → 跳过冻结）
    dispatcher
        .dispatch_segment_activate("u1", "T1", "act6", &new_gen_seg.id)
        .await
        .unwrap();

    let segments = store
        .list_message_segments("act6", "default")
        .await
        .unwrap();
    assert_eq!(segments.len(), 2, "来回切换段数不增长（同内容不重复冻结）");
    let rows = sqlite
        .get_slot_messages_by_pipeline("act6", "default", MessageQueryOpts::default())
        .unwrap();
    let got: Vec<String> = rows
        .iter()
        .map(|r| r.content_preview.clone().unwrap_or_default())
        .collect();
    assert_eq!(
        got,
        vec!["问".to_string(), "新A".into(), "新B".into(), "新C".into()],
        "查重跳过冻结不影响目标段写回"
    );
    assert_eq!(
        frames_of_type(&frames.lock().unwrap(), "segment_activated").len(),
        2,
        "每跳 ack 照发（多端对账）"
    );
}

#[tokio::test]
async fn segment_activate_write_back_preserves_run_id_and_message_id() {
    // 段成员携带身份与 run 审计锚（修复后 load_message_history 的冻结形态）→
    // 激活写回后槽位行保住 run_id/message_id。缺了它们：写回行 run_id=NULL →
    // regenerate 审计门禁拒「目标 user 消息无有效 run_id」且前端失去稳定
    // 消息 id（用户实证：激活后重试被拒、前端白屏）。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let old_a =
        json!({"role": "assistant", "content": "旧A", "message_id": "a_old", "run_id": "run-old"});
    store
        .apply_messages_ops_to_table(
            "act7",
            "default",
            &[
                json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "问"}, "_message_id": "m_q", "_run_id": "run-old"}),
                json!({"op": "set", "seq": 1, "msg": old_a, "_message_id": "a_old", "_run_id": "run-old"}),
                json!({
                    "op": "freeze_segment", "id": "seg_gen1",
                    "base_seq": 1, "base_len": 1, "visible_to": "",
                    "preview": "旧A", "members": [old_a],
                }),
                // 当前代（重试的新内容覆盖 seq1）
                json!({"op": "set", "seq": 1, "msg": {"role": "assistant", "content": "新A"}, "_message_id": "a_new", "_run_id": "run-new"}),
            ],
        )
        .await
        .unwrap();
    store
        .create_session(&session_record(Some("act7")))
        .await
        .unwrap();
    store
        .link_pipeline_session("act7", "T1", "default")
        .await
        .unwrap();
    seed_default_user(&*store, "u1").await;
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    let dispatcher = segment_dispatcher(&store, &frames);

    dispatcher
        .dispatch_segment_activate("u1", "T1", "act7", "seg_gen1")
        .await
        .unwrap();

    let rows = sqlite
        .get_slot_messages_by_pipeline("act7", "default", MessageQueryOpts::default())
        .unwrap();
    let seq1 = rows.iter().find(|r| r.seq_in_branch == 1).expect("seq1 行");
    assert_eq!(seq1.content_preview.as_deref(), Some("旧A"), "目标段写回");
    assert_eq!(seq1.message_id, "a_old", "record_id 随段成员还原");
    assert_eq!(seq1.run_id, "run-old", "run 审计锚随段成员还原");
}

#[tokio::test]
async fn regenerate_after_segment_activation_not_rejected_for_missing_run_id() {
    // 用户实证链：激活写回后，**段范围内的目标 user 消息**槽位行 run_id=NULL →
    // regenerate 审计门禁拒「目标 user 消息无有效 run_id(审计不可见)」→ 前端
    // 已本地截断 → 白屏。修复后（冻结携带身份与审计锚、写回还原）：激活后的
    // 重试放行。目标 = 段内最后一条 user 消息（与用户场景同构：重试总是打在
    // 最后一条 user 上，而它正是被激活覆写的槽位）。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let old_a =
        json!({"role": "assistant", "content": "旧A", "message_id": "a_old", "run_id": "run-old"});
    let target_q = json!({"role": "user", "content": "现在你知道这个数字是啥吗", "message_id": "m_q2", "run_id": "run-old"});
    store
        .apply_messages_ops_to_table(
            "act8",
            "default",
            &[
                json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "问"}, "_message_id": "m_q0", "_run_id": "run-old"}),
                json!({"op": "set", "seq": 1, "msg": {"role": "assistant", "content": "想好啦"}, "_message_id": "a_q0", "_run_id": "run-old"}),
                json!({"op": "set", "seq": 2, "msg": target_q, "_message_id": "m_q2", "_run_id": "run-old"}),
                json!({"op": "set", "seq": 3, "msg": old_a, "_message_id": "a_old", "_run_id": "run-old"}),
                json!({
                    "op": "freeze_segment", "id": "seg_gen1",
                    "base_seq": 2, "base_len": 2, "visible_to": "",
                    "preview": "现在你知道这个数字是啥吗", "members": [target_q, old_a],
                }),
                // 当前代（重试的新内容覆写段范围内槽位）
                json!({"op": "set", "seq": 2, "msg": {"role": "user", "content": "现在你知道这个数字是啥吗"}, "_message_id": "m_q2b", "_run_id": "run-new"}),
                json!({"op": "set", "seq": 3, "msg": {"role": "assistant", "content": "新A"}, "_message_id": "a_new", "_run_id": "run-new"}),
            ],
        )
        .await
        .unwrap();
    store
        .create_session(&session_record(Some("act8")))
        .await
        .unwrap();
    store
        .link_pipeline_session("act8", "T1", "default")
        .await
        .unwrap();
    seed_default_user(&*store, "u1").await;
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    let dispatcher = segment_dispatcher(&store, &frames);

    dispatcher
        .dispatch_segment_activate("u1", "T1", "act8", "seg_gen1")
        .await
        .unwrap();

    // 写回后 seq2 行保住身份与审计锚（修复前 = NULL → 门禁拒）
    let seq2 = sqlite
        .get_slot_messages_by_pipeline("act8", "default", MessageQueryOpts::default())
        .unwrap()
        .into_iter()
        .find(|m| m.seq_in_branch == 2)
        .expect("seq2 行");
    assert_eq!(seq2.message_id, "m_q2", "目标 user 消息 record_id 还原");
    assert_eq!(seq2.run_id, "run-old", "目标 user 消息 run 审计锚还原");

    // 修复前这里 Err("regenerate 拒绝: 目标 user 消息无有效 run_id(审计不可见)")
    dispatcher
        .dispatch_regenerate("u1", "T1", "act8", &seq2.message_id, None)
        .await
        .unwrap();
}

#[tokio::test]
async fn segment_activate_rejected_while_run_running() {
    // 运行态守卫（§7.3-1）：run 活跃期间拒绝激活（后缀替换会与引擎 append
    // 竞争），段表与槽位原样。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    seed_activate_history(&store, "act4").await;
    seed_default_user(&*store, "u1").await;
    store
        .record_run_start(
            "act4",
            "default",
            "run-act4",
            "cfg",
            "2026-10-15T00:00:00+00:00",
        )
        .await
        .unwrap();
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    let dispatcher = segment_dispatcher(&store, &frames);

    let err = dispatcher
        .dispatch_segment_activate("u1", "T1", "act4", "seg_gen1")
        .await
        .unwrap_err();
    assert!(err.contains("任务运行中"), "拒绝原因: {err}");
    assert_eq!(
        store
            .list_message_segments("act4", "default")
            .await
            .unwrap()
            .len(),
        1,
        "拒绝时不建新段"
    );
    let rows = sqlite
        .get_slot_messages_by_pipeline("act4", "default", MessageQueryOpts::default())
        .unwrap();
    assert_eq!(rows.len(), 4, "拒绝时槽位原样");
    assert!(
        frames_of_type(&frames.lock().unwrap(), "segment_activated").is_empty(),
        "拒绝时无 ack"
    );
}

#[tokio::test]
async fn list_error_returns_frontend_value_not_main_pipeline() {
    // 存储故障（Err）与"前端值不属于该 thread"（校验失败）同属拒绝路径：
    // 不回落 session.active_pipeline_id、不改写为主管道（2026-08-22 裁决，
    // 写错桶 = 子管道消息记到主管道）。
    let mock = ResolveMock::new(
        Err(StorageError::NotFound("mock query failure".to_string())),
        Some(session_record(Some("P-main"))),
    );
    assert_eq!(resolve(mock, "T1", "P-sub").await, "P-sub");
}

#[tokio::test]
async fn frontend_not_member_rejected_not_snapped_to_main() {
    // 前端值不属于该 thread（校验失败）→ 空串由调用方显式拒绝，
    // 绝不静默换管道写进主管道（2026-08-30 管道身份裁定：拿别的管道当回退=写错桶）。
    let mock = ResolveMock::new(
        Ok(vec!["P-other".to_string()]),
        Some(session_record(Some("P-main"))),
    );
    assert_eq!(resolve(mock, "T1", "P-sub").await, "");
}

#[tokio::test]
async fn frontend_member_trusted() {
    let mock = ResolveMock::new(
        Ok(vec!["P-sub".to_string(), "P-main".to_string()]),
        Some(session_record(Some("P-main"))),
    );
    assert_eq!(resolve(mock, "T1", "P-sub").await, "P-sub");
}

#[tokio::test]
async fn empty_frontend_uses_main_pipeline() {
    let mock = ResolveMock::new(Ok(vec![]), Some(session_record(Some("P-main"))));
    assert_eq!(resolve(mock, "T1", "").await, "P-main");
}

#[tokio::test]
async fn no_session_yields_empty_not_thread_id() {
    // 会话 id 是组织集合 id，绝不充当管道执行坐标回填（2026-08-30 管道身份裁定）。
    // 解析不出真实管道 → 空串，由调用方显式报错/幂等跳过。
    let mock = ResolveMock::new(Ok(vec![]), None);
    assert_eq!(resolve(mock, "T1", "").await, "");
}
