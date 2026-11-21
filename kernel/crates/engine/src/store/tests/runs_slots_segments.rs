// @feature: FP-0.2.一 插件协议（runs slots segments 存储面） | @ci: rust-test
use super::*;

// ── runs 生命周期：主动排空 / 崩溃清扫 / 挂起元数据检索 ──

/// G8 主动排空：running → suspended 计数正确；已终态 run 不受影响；
/// 二次调用返回 0（幂等）。与 reap（→failed）语义分离。
#[tokio::test]
async fn suspend_running_runs_only_touches_running() {
    let store = SqliteStore::open_memory().unwrap();
    store
        .record_run_start(
            "pipe_s1",
            "default",
            "run_s1",
            "h",
            "2026-10-15T00:00:00+00:00",
        )
        .unwrap();
    store
        .record_run_start(
            "pipe_s2",
            "default",
            "run_s2",
            "h",
            "2026-10-15T00:00:00+00:00",
        )
        .unwrap();
    store
        .record_run_start(
            "pipe_done",
            "default",
            "run_done",
            "h",
            "2026-10-15T00:00:00+00:00",
        )
        .unwrap();
    store
        .set_run_status_projection("pipe_done", "default", RunStatus::Completed)
        .unwrap();

    assert_eq!(
        store.suspend_running_runs("test: g8 drain").unwrap(),
        2,
        "两条 running 被排空"
    );
    assert_eq!(
        store.get_run("run_s1").await.unwrap().status,
        RunStatus::Suspended
    );
    assert_eq!(
        store.get_run("run_done").await.unwrap().status,
        RunStatus::Completed,
        "已终态 run 不被改写"
    );
    assert_eq!(
        store.suspend_running_runs("test: g8 drain").unwrap(),
        0,
        "重入幂等"
    );
}

/// P-23 片1（G8 排空写挂起归因）：排空行同批落 `suspended_reason` = 触发源
/// 区分的 reason 字符串；非排空挂起行（W2 审批挂起先行落库）不写该键——
/// 键缺席 = 非 drain 挂起，归因行回退缺席。空串语义 = 未署名
/// （run_stop_reason「未署名轮落空串防跨 run 残留」先例同款）：再次排空
/// 覆盖旧值不残留。
#[tokio::test]
async fn suspend_running_runs_writes_suspended_reason_only_to_drained_rows() {
    let store = SqliteStore::open_memory().unwrap();
    let reason = "memory watermark self-heal: private bytes 持续超阈值";
    store
        .record_run_start(
            "pipe_dr1",
            "default",
            "run_dr1",
            "h",
            "2026-11-04T00:00:00+00:00",
        )
        .unwrap();
    store
        .record_run_start(
            "pipe_dr2",
            "default",
            "run_dr2",
            "h",
            "2026-11-04T00:01:00+00:00",
        )
        .unwrap();
    // W2 审批挂起先行落库（非 drain 挂起路径）：翻 suspended + 落凭据
    store
        .record_run_start(
            "pipe_w2",
            "default",
            "run_w2",
            "h",
            "2026-11-04T00:02:00+00:00",
        )
        .unwrap();
    store
        .set_run_status_projection("pipe_w2", "default", RunStatus::Suspended)
        .unwrap();
    store
        .upsert_state_field("pipe_w2", "default", "suspend_request_id", &json!("req-7"))
        .unwrap();

    let raw_reason = |pid: &str| -> Option<(String, String)> {
        store
            .conn
            .lock()
            .query_row(
                "SELECT value, value_kind FROM pipeline_state \
                 WHERE pipeline_id = ?1 AND field_key = 'suspended_reason' \
                 AND tenant_id = 'default'",
                rusqlite::params![pid],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .ok()
    };

    assert_eq!(
        store.suspend_running_runs(reason).unwrap(),
        2,
        "两条 running 被排空"
    );
    for pid in ["pipe_dr1", "pipe_dr2"] {
        let (value, kind) = raw_reason(pid)
            .unwrap_or_else(|| panic!("排空行 {pid} 必须落 suspended_reason 归因键"));
        assert_eq!(value, reason, "{pid} 归因值 = 触发源 reason");
        assert_eq!(kind, "str", "归因是字符串标量");
    }
    assert!(
        raw_reason("pipe_w2").is_none(),
        "非 drain 挂起行（W2 审批挂起）不得被写归因键"
    );

    // 未署名（空串）覆盖防跨 run 残留：dr1 复位 running 后以空串
    // reason 再排空
    store
        .upsert_state_field("pipe_dr1", "default", "run_status", &json!("running"))
        .unwrap();
    assert_eq!(
        store.suspend_running_runs("").unwrap(),
        1,
        "复位行再排空（空串 reason = 未署名）"
    );
    let (value, _) = raw_reason("pipe_dr1").expect("二次排空仍落键");
    assert_eq!(value, "", "空串 reason = 未署名，覆盖旧值防残留");
}

/// 挂起运行的挂起凭据写入与按 request_id 检索：suspend_request_id state 键
/// 可被找回，RunRecord.metadata 按 pending_interaction_request_id 合成，
/// 投影携带所属管道（供恢复路径簿记回写）；未匹配 request_id 返回 None。
#[tokio::test]
async fn suspend_request_id_findable_by_request_id() {
    let store = SqliteStore::open_memory().unwrap();
    store
        .record_run_start(
            "pipe_meta",
            "default",
            "run_meta",
            "h",
            "2026-10-15T00:00:00+00:00",
        )
        .unwrap();
    store
        .set_run_status_projection("pipe_meta", "default", RunStatus::Suspended)
        .unwrap();
    store
        .upsert_state_field(
            "pipe_meta",
            "default",
            "suspend_request_id",
            &json!("req-42"),
        )
        .unwrap();

    let found = store
        .find_suspended_run_by_request_id("req-42")
        .unwrap()
        .expect("挂起 run 必须按 request_id 找回");
    assert_eq!(found.run_id, "run_meta");
    assert_eq!(found.status, RunStatus::Suspended);
    assert_eq!(
        found.pipeline_id.as_deref(),
        Some("pipe_meta"),
        "投影携带所属管道供簿记回写"
    );
    assert_eq!(
        found.metadata,
        Some(json!({"pending_interaction_request_id": "req-42"})),
        "suspend_request_id 按 pending_interaction_request_id 合成 metadata"
    );

    assert!(
        store
            .find_suspended_run_by_request_id("no-such-req")
            .unwrap()
            .is_none(),
        "不匹配的 request_id 不得命中"
    );

    // 未携带 suspend_request_id 的另一个挂起 run：不得误命中，也不挤占检索结果
    store
        .record_run_start(
            "pipe_no_cred",
            "default",
            "run_no_cred",
            "h",
            "2026-10-15T00:00:00+00:00",
        )
        .unwrap();
    store
        .set_run_status_projection("pipe_no_cred", "default", RunStatus::Suspended)
        .unwrap();
    assert!(
        store
            .find_suspended_run_by_request_id("req-42")
            .unwrap()
            .is_some_and(|r| r.run_id == "run_meta"),
        "无凭据挂起 run 不得挤占检索结果"
    );
}

/// 崩溃清扫的收敛语义（state 唯一真值）：残留 running（轮中派发的过期值）
/// → failed + 补 run_ended_at；挂起（非终态）与已有终态投影一律不触碰。
/// 统一管道标题出口（ADR 2026-10-27-pipeline-title-field）：runs 快照带
/// state.title；无 title 键的管道不出该字段（无回退腿，消费方走 ID 兜底）。
#[tokio::test]
async fn list_pipelines_carries_unified_title_and_omits_when_absent() {
    let store = SqliteStore::open_memory().unwrap();
    store
        .record_run_start(
            "pipe_task",
            "default",
            "run_t",
            "h",
            "2026-10-27T00:00:00+00:00",
        )
        .unwrap();
    store
        .record_run_start(
            "pipe_bare",
            "default",
            "run_b",
            "h",
            "2026-10-27T00:01:00+00:00",
        )
        .unwrap();
    // 启用方出生写入统一标题键（任务管道：出生协议；会话主管道：会话域刷新）
    let mut fields = serde_json::Map::new();
    fields.insert("title".to_string(), serde_json::json!("周报整理"));
    store
        .upsert_state_fields("pipe_task", "default", &fields)
        .unwrap();

    let rows = store.list_pipelines_inner("default", None, 100).unwrap();
    assert_eq!(rows.len(), 2, "两条 run 都在快照");
    let titled = rows
        .iter()
        .find(|r| r.pipeline_id.as_deref() == Some("pipe_task"))
        .expect("任务管道行在快照");
    assert_eq!(titled.title.as_deref(), Some("周报整理"), "标题随快照出口");
    let bare = rows
        .iter()
        .find(|r| r.pipeline_id.as_deref() == Some("pipe_bare"))
        .expect("无标题管道行在快照");
    assert!(bare.title.is_none(), "无 title 键不出字段（无回退腿）");
}

#[tokio::test]
async fn reap_orphan_runs_fixes_run_status_projection_three_states() {
    let store = SqliteStore::open_memory().unwrap();

    // ① 残留 running → failed；② 挂起（非 running 非终态）不触碰；
    // ③ 已是 completed（真值）不覆盖
    store
        .record_run_start(
            "pipe_stale",
            "default",
            "run_stale",
            "h",
            "2026-10-15T00:00:00+00:00",
        )
        .unwrap();
    store
        .record_run_start(
            "pipe_susp",
            "default",
            "run_susp",
            "h",
            "2026-10-15T00:00:00+00:00",
        )
        .unwrap();
    store
        .set_run_status_projection("pipe_susp", "default", RunStatus::Suspended)
        .unwrap();
    store
        .record_run_start(
            "pipe_done",
            "default",
            "run_done",
            "h",
            "2026-10-15T00:00:00+00:00",
        )
        .unwrap();
    store
        .set_run_status_projection("pipe_done", "default", RunStatus::Completed)
        .unwrap();

    let reaped = store.reap_orphan_runs().unwrap();
    assert_eq!(
        reaped,
        vec!["pipe_stale".to_string()],
        "仅残留 running 的投影被清扫，受害者清单点名"
    );
    let raw_status = |pid: &str| -> Option<String> {
        store
            .conn
            .lock()
            .query_row(
                "SELECT value FROM pipeline_state \
                     WHERE pipeline_id = ?1 AND field_key = 'run_status' AND tenant_id = 'default'",
                rusqlite::params![pid],
                |r| r.get(0),
            )
            .ok()
    };
    assert_eq!(
        raw_status("pipe_stale").as_deref(),
        Some("failed"),
        "残留 running 纠偏为 failed（标量 str 原文）"
    );
    let ended: String = store
            .conn
            .lock()
            .query_row(
                "SELECT value FROM pipeline_state \
                 WHERE pipeline_id = 'pipe_stale' AND field_key = 'run_ended_at' AND tenant_id = 'default'",
                [],
                |r| r.get(0),
            )
            .unwrap();
    assert!(!ended.is_empty(), "被清扫管道补 run_ended_at");
    assert_eq!(
        raw_status("pipe_susp").as_deref(),
        Some("suspended"),
        "挂起投影不归清扫管"
    );
    assert_eq!(
        raw_status("pipe_done").as_deref(),
        Some("completed"),
        "已有终态不覆盖"
    );
    assert!(store.reap_orphan_runs().unwrap().is_empty(), "重入幂等");
}

/// 崩溃清扫署名（ADR 2026-11-12-crash-swept-running-tasks-auto-relaunch）：
/// 被清扫的受害行补 `run_stop_reason=crash_swept`——任务插件 boot 自动恢复
/// 凭此标记区分「内核重启打断的 running」与「重启前就真失败」，只自动拉起
/// 前者。非受害行（真终态 failed 自带归因 / suspended / completed）不署名、
/// 既有归因不被覆盖；重入不重复署名。
#[tokio::test]
async fn reap_orphan_runs_signs_victims_with_crash_swept_reason() {
    let store = SqliteStore::open_memory().unwrap();

    // 受害行：残留 running（内核死亡时在飞）
    store
        .record_run_start(
            "pipe_stale",
            "default",
            "run_stale",
            "h",
            "2026-10-15T00:00:00+00:00",
        )
        .unwrap();
    // 对照行①：重启前就真失败（带真实归因，不得被署名覆盖）
    store
        .record_run_start(
            "pipe_realfail",
            "default",
            "run_rf",
            "h",
            "2026-10-15T00:00:00+00:00",
        )
        .unwrap();
    store
        .set_run_status_projection("pipe_realfail", "default", RunStatus::Failed)
        .unwrap();
    store
        .conn
        .lock()
        .execute(
            "INSERT INTO pipeline_state (pipeline_id, field_key, value, value_kind, tenant_id, updated_at) \
             VALUES ('pipe_realfail', 'run_stop_reason', 'llm_fail_loop', 'str', 'default', '2026-10-15T00:00:01+00:00')",
            [],
        )
        .unwrap();

    let reaped = store.reap_orphan_runs().unwrap();
    assert_eq!(
        reaped,
        vec!["pipe_stale".to_string()],
        "仅 running 残留被清扫"
    );

    let reason = |pid: &str| -> Option<String> {
        store
            .conn
            .lock()
            .query_row(
                "SELECT value FROM pipeline_state \
                 WHERE pipeline_id = ?1 AND field_key = 'run_stop_reason' AND tenant_id = 'default'",
                rusqlite::params![pid],
                |r| r.get(0),
            )
            .ok()
    };
    assert_eq!(
        reason("pipe_stale").as_deref(),
        Some("crash_swept"),
        "受害行署名 crash_swept（自动恢复的唯一判别键）"
    );
    assert_eq!(
        reason("pipe_realfail").as_deref(),
        Some("llm_fail_loop"),
        "真终态行的既有归因不被清扫覆盖"
    );
    assert!(store.reap_orphan_runs().unwrap().is_empty(), "重入幂等");
    assert_eq!(
        reason("pipe_stale").as_deref(),
        Some("crash_swept"),
        "重入不改署名（不重复写）"
    );
}

// ── message_slots op 写入：insert 顺延 / delete 留 gap / 未知 op 忽略 ──

/// op 语义：insert 在位置 N 插入并让后段 seq 顺延（message_id 不变）；
/// delete 留 gap（后段 seq 不动）；unknown op 静默忽略。
/// 对照两组输入（插入中间位 vs 插入队首）验证顺延的普适性。
#[tokio::test]
async fn apply_messages_ops_insert_shifts_delete_leaves_gap() {
    let store = SqliteStore::open_memory().unwrap();
    let pid = "pipe-ops";
    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[
                json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "q"}}),
                json!({"op": "set", "seq": 1, "msg": {"role": "assistant", "content": "a"}}),
                json!({"op": "set", "seq": 2, "msg": {"role": "user", "content": "q2"}}),
            ],
        )
        .unwrap();
    let ids_before: Vec<(i64, String)> = {
        let conn = store.conn.lock();
        let mut stmt = conn
            .prepare("SELECT seq, message_id FROM message_slots WHERE pipeline_id=?1 ORDER BY seq")
            .unwrap();
        stmt.query_map([pid], |r| Ok((r.get(0)?, r.get(1)?)))
            .unwrap()
            .map(|r| r.unwrap())
            .collect()
    };
    assert_eq!(ids_before.len(), 3);

    // insert at=1：后段（原 seq 1/2）顺延到 2/3，message_id 保持不变
    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[json!({"op": "insert", "at": 1, "msg": {"role": "user", "content": "mid"}})],
        )
        .unwrap();
    let after: Vec<(i64, String)> = {
        let conn = store.conn.lock();
        let mut stmt = conn
            .prepare("SELECT seq, message_id FROM message_slots WHERE pipeline_id=?1 ORDER BY seq")
            .unwrap();
        stmt.query_map([pid], |r| Ok((r.get(0)?, r.get(1)?)))
            .unwrap()
            .map(|r| r.unwrap())
            .collect()
    };
    assert_eq!(after.len(), 4, "插入后槽位 +1");
    assert_eq!(after[1].0, 1, "插入位就是 at");
    assert_eq!(after[2].0, 2);
    assert_eq!(
        after[2].1, ids_before[1].1,
        "后段原 seq=1 的 id 不变，仅顺延"
    );
    assert_eq!(after[3].1, ids_before[2].1, "后段原 seq=2 的 id 不变");

    // delete 中间槽（msg=null）→ 留 gap：后段 seq 与 id 都不动
    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[json!({"op": "set", "seq": 1, "msg": serde_json::Value::Null})],
        )
        .unwrap();
    let after_del: Vec<(i64, String)> = {
        let conn = store.conn.lock();
        let mut stmt = conn
            .prepare("SELECT seq, message_id FROM message_slots WHERE pipeline_id=?1 ORDER BY seq")
            .unwrap();
        stmt.query_map([pid], |r| Ok((r.get(0)?, r.get(1)?)))
            .unwrap()
            .map(|r| r.unwrap())
            .collect()
    };
    assert_eq!(after_del.len(), 3, "删除后槽位数 -1");
    assert_eq!(after_del[1].0, 2, "gap 保留（后段 seq 不顺延）");
    assert_eq!(after_del[2].0, 3);
    assert_eq!(after_del[2].1, after[3].1, "后段 id 不受删除影响");

    // 未知 op 与缺参 op：忽略，不报错不改表
    let count_before = after_del.len();
    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[
                json!({"op": "future_op", "seq": 9}),
                json!({"op": "set"}),
                json!({"op": "insert", "at": 5}),
            ],
        )
        .unwrap();
    let count_after: i64 = store
        .conn
        .lock()
        .query_row(
            "SELECT COUNT(*) FROM message_slots WHERE pipeline_id=?1",
            [pid],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(
        count_after as usize, count_before,
        "未知/缺参 op 不落任何槽位"
    );
}

/// 插入队首（at=0）的第二组输入：全体顺延，原有顺序与身份保持。
#[tokio::test]
async fn apply_messages_ops_insert_at_head_preserves_order_and_identity() {
    let store = SqliteStore::open_memory().unwrap();
    let pid = "pipe-head";
    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[
                json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "a"}}),
                json!({"op": "set", "seq": 1, "msg": {"role": "assistant", "content": "b"}}),
            ],
        )
        .unwrap();
    let before: Vec<String> = {
        let conn = store.conn.lock();
        let mut stmt = conn
            .prepare("SELECT message_id FROM message_slots WHERE pipeline_id=?1 ORDER BY seq")
            .unwrap();
        stmt.query_map([pid], |r| r.get::<_, String>(0))
            .unwrap()
            .map(|r| r.unwrap())
            .collect()
    };

    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[json!({"op": "insert", "at": 0, "msg": {"role": "user", "content": "head"}})],
        )
        .unwrap();
    let after: Vec<String> = {
        let conn = store.conn.lock();
        let mut stmt = conn
            .prepare("SELECT message_id FROM message_slots WHERE pipeline_id=?1 ORDER BY seq")
            .unwrap();
        stmt.query_map([pid], |r| r.get::<_, String>(0))
            .unwrap()
            .map(|r| r.unwrap())
            .collect()
    };
    assert_eq!(after.len(), before.len() + 1);
    assert_eq!(
        &after[1..],
        before.as_slice(),
        "队首插入后原有身份与顺序不变"
    );
}

// ── 域12：消息段（freeze_segment op / 段表读路径 / 清扫 / 级联）──

/// 段行数与 blob 总数快照（幂等/去重断言用）。
fn segment_row_count(store: &SqliteStore) -> i64 {
    store
        .conn
        .lock()
        .query_row("SELECT COUNT(*) FROM message_segments", [], |r| r.get(0))
        .unwrap()
}

fn blob_count(store: &SqliteStore) -> i64 {
    store
        .conn
        .lock()
        .query_row("SELECT COUNT(*) FROM blobs", [], |r| r.get(0))
        .unwrap()
}

/// freeze_segment op：重复投递不重复建行（段行按 id 幂等）；成员 blob 与
/// 槽位 blob 内容寻址去重（同文一条一存）；段行字段与 op 一致；
/// members_blob 解析 = 成员 blob_id 数组且首成员与槽位 blob 同 id。
#[tokio::test]
async fn freeze_segment_op_idempotent_with_blob_dedup() {
    let store = SqliteStore::open_memory().unwrap();
    let pid = "pipe-seg";
    let user = json!({"role": "user", "content": "第一问"});
    let assistant = json!({"role": "assistant", "content": "旧回复"});
    let freeze = json!({
        "op": "freeze_segment",
        "id": "seg_fixed_id",
        "base_seq": 1,
        "base_len": 1,
        "visible_to": "",
        "preview": "旧回复",
        "members": [assistant],
    });
    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[
                json!({"op": "set", "seq": 0, "msg": user}),
                json!({"op": "set", "seq": 1, "msg": assistant}),
                freeze.clone(),
            ],
        )
        .unwrap();
    assert_eq!(segment_row_count(&store), 1, "首投递建一行");

    // 重复投递同一 op：段行不重复、blob 不新增（ON CONFLICT DO NOTHING +
    // 内容寻址去重双兜底）
    store
        .apply_messages_ops_to_table(pid, "default", &[freeze])
        .unwrap();
    assert_eq!(segment_row_count(&store), 1, "重复投递不重复建行");

    // blob 总账：slot 两成员 blob（q/a）+ 段引用列表 blob = 3；
    // 冻结成员与槽位同文 → 去重后不新增第 4 条
    assert_eq!(
        blob_count(&store),
        3,
        "成员 blob 与槽位去重 + 引用列表 1 条"
    );

    let (base_seq, base_len, visible_to, preview): (i64, i64, String, String) = store
        .conn
        .lock()
        .query_row(
            "SELECT base_seq, base_len, visible_to, preview FROM message_segments \
                 WHERE id = 'seg_fixed_id'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .unwrap();
    assert_eq!((base_seq, base_len), (1, 1));
    assert_eq!(visible_to, "");
    assert_eq!(preview, "旧回复");

    // members_blob 解析 = 成员 blob_id 数组；首成员与 seq1 槽位 blob 同 id（去重证据）
    let members_blob_id: String = store
        .conn
        .lock()
        .query_row(
            "SELECT members_blob_id FROM message_segments WHERE id = 'seg_fixed_id'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    let slot_blob: String = store
        .conn
        .lock()
        .query_row(
            "SELECT blob_id FROM message_slots WHERE pipeline_id=?1 AND seq=1",
            [pid],
            |r| r.get(0),
        )
        .unwrap();
    let ids: Vec<String> = {
        let conn = store.conn.lock();
        let data: Vec<u8> = conn
            .query_row(
                "SELECT data FROM blobs WHERE blob_id = ?1",
                [&members_blob_id],
                |r| r.get(0),
            )
            .unwrap();
        serde_json::from_slice(&data).unwrap()
    };
    assert_eq!(ids, vec![slot_blob], "引用列表 = 成员 blob_id 数组");
}

/// 畸形 freeze_segment（base_len 与成员数不一致）：整 op 跳过，不建行不报错
/// （与缺参 op 同款前向兼容语义）。
#[tokio::test]
async fn freeze_segment_op_malformed_base_len_is_skipped() {
    let store = SqliteStore::open_memory().unwrap();
    store
        .apply_messages_ops_to_table(
            "pipe-bad",
            "default",
            &[json!({
                "op": "freeze_segment",
                "id": "seg_bad",
                "base_seq": 0,
                "base_len": 5,
                "visible_to": "",
                "members": [{"role": "user", "content": "x"}],
            })],
        )
        .unwrap();
    assert_eq!(
        segment_row_count(&store),
        0,
        "base_len 与成员数不一致不建行"
    );
}

/// insert 后段坐标同事务后移（§2.4）：base_seq ≥ at 的分支段统一 +1，
/// 跨骑段（base_seq < at < base_seq+base_len）锚不动；槽位与段坐标在
/// 同一显式事务内一致变化。
#[tokio::test]
async fn insert_op_shifts_segment_base_seq_in_same_transaction() {
    let store = SqliteStore::open_memory().unwrap();
    let pid = "pipe-shift";
    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[
                json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "m0"}}),
                json!({"op": "set", "seq": 1, "msg": {"role": "assistant", "content": "m1"}}),
                json!({"op": "set", "seq": 2, "msg": {"role": "user", "content": "m2"}}),
                json!({"op": "set", "seq": 3, "msg": {"role": "assistant", "content": "m3"}}),
                // ahead：分支段（base_seq=2 ≥ 后续插入位 2）；straddle：跨骑段（1 < 2 < 1+3）
                json!({
                    "op": "freeze_segment", "id": "seg_ahead",
                    "base_seq": 2, "base_len": 2,
                    "visible_to": "", "members":
                        [{"role": "user", "content": "m2"}, {"role": "assistant", "content": "m3"}]
                }),
                json!({
                    "op": "freeze_segment", "id": "seg_straddle",
                    "base_seq": 1, "base_len": 3,
                    "visible_to": "", "members":
                        [{"role": "assistant", "content": "m1"},
                         {"role": "user", "content": "m2"},
                         {"role": "assistant", "content": "m3"}]
                }),
            ],
        )
        .unwrap();

    // insert at=2：槽位 seq≥2 后移 +1，段坐标同事务同步
    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[json!({"op": "insert", "at": 2, "msg": {"role": "user", "content": "mid"}})],
        )
        .unwrap();

    let slots: Vec<i64> = {
        let conn = store.conn.lock();
        let mut stmt = conn
            .prepare("SELECT seq FROM message_slots WHERE pipeline_id=?1 ORDER BY seq")
            .unwrap();
        stmt.query_map([pid], |r| r.get::<_, i64>(0))
            .unwrap()
            .map(|r| r.unwrap())
            .collect()
    };
    assert_eq!(slots, vec![0, 1, 2, 3, 4], "插入后槽位顺延");
    let (ahead, straddle): (i64, i64) = store
        .conn
        .lock()
        .query_row(
            "SELECT MAX(CASE WHEN id='seg_ahead' THEN base_seq END), \
                        MAX(CASE WHEN id='seg_straddle' THEN base_seq END) \
                 FROM message_segments WHERE pipeline_id = ?1",
            [pid],
            |r| {
                Ok((
                    r.get::<_, Option<i64>>(0)?.unwrap(),
                    r.get::<_, Option<i64>>(1)?.unwrap(),
                ))
            },
        )
        .unwrap();
    assert_eq!(ahead, 3, "base_seq ≥ at 的分支段同步 +1");
    assert_eq!(straddle, 1, "跨骑段锚不动");
}

/// 段读路径：list 不含成员、含 preview、按 (base_seq, created_at) 升序、
/// tenant 过滤；get 解析成员全文（与冻结原文一致）；不存在/跨租户 → None。
#[tokio::test]
async fn list_and_get_message_segments_resolve_members() {
    let store = SqliteStore::open_memory().unwrap();
    let pid = "pipe-read";
    let later = json!({
        "op": "freeze_segment", "id": "seg_later",
        "base_seq": 2, "base_len": 1, "visible_to": "",
        "preview": "后段", "members": [{"role": "assistant", "content": "后段回复"}],
    });
    let earlier = json!({
        "op": "freeze_segment", "id": "seg_earlier",
        "base_seq": 1, "base_len": 1, "visible_to": "[user]",
        "preview": "原文", "members": [{"role": "user", "content": "原文"}],
    });
    let other_tenant = json!({
        "op": "freeze_segment", "id": "seg_t2",
        "base_seq": 0, "base_len": 1, "visible_to": "",
        "preview": "他租", "members": [{"role": "user", "content": "他租"}],
    });
    store
        .apply_messages_ops_to_table(pid, "default", &[later.clone(), earlier.clone()])
        .unwrap();
    store
        .apply_messages_ops_to_table(pid, "tenant_2", &[other_tenant])
        .unwrap();

    let rows = store.list_message_segments(pid, "default").await.unwrap();
    assert_eq!(
        rows.iter().map(|r| r.id.as_str()).collect::<Vec<_>>(),
        vec!["seg_earlier", "seg_later"],
        "按 base_seq 升序，tenant 过滤"
    );
    assert!(rows.iter().all(|r| r.members.is_none()), "list 不解析成员");
    assert_eq!(rows[0].preview.as_deref(), Some("原文"));
    assert_eq!(rows[0].visible_to, "[user]");

    let one = store
        .get_message_segment("seg_later", "default")
        .await
        .unwrap()
        .expect("存在的段必须取到");
    assert_eq!(one.base_seq, 2);
    assert_eq!(
        one.members.clone().unwrap(),
        vec![json!({"role": "assistant", "content": "后段回复"})],
        "get 解析成员全文 = 冻结原文"
    );
    assert!(
        store
            .get_message_segment("seg_t2", "default")
            .await
            .unwrap()
            .is_none(),
        "跨租户不可见"
    );
    assert!(
        store
            .get_message_segment("seg_ghost", "default")
            .await
            .unwrap()
            .is_none(),
        "不存在的段返回 None"
    );
}

/// 清扫联动（§4 必改）：槽位删除后，段成员 blob 与段引用列表 blob 是唯一
/// 持有方——清扫必须保留；无任何引用的真孤儿照常回收。
#[tokio::test]
async fn purge_orphan_blobs_keeps_segment_member_blobs() {
    let store = SqliteStore::open_memory().unwrap();
    let pid = "pipe-purge";
    let assistant = json!({"role": "assistant", "content": "被删后缀"});
    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[
                json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "留在场内"}}),
                json!({"op": "set", "seq": 1, "msg": assistant}),
                json!({
                    "op": "freeze_segment", "id": "seg_keep",
                    "base_seq": 1, "base_len": 1, "visible_to": "",
                    "preview": "被删后缀", "members": [assistant],
                }),
            ],
        )
        .unwrap();
    // 真孤儿：无任何引用
    store
        .store_blob(b"orphan".as_slice(), "text/plain")
        .unwrap();
    // 删除槽位 1：成员 blob 只剩段引用列表持有
    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[json!({"op": "set", "seq": 1, "msg": serde_json::Value::Null})],
        )
        .unwrap();

    let deleted = store.purge_orphan_blobs().await.unwrap();
    assert_eq!(deleted, 1, "仅真孤儿被清扫");

    let remaining: Vec<String> = {
        let conn = store.conn.lock();
        let mut stmt = conn.prepare("SELECT blob_id FROM blobs").unwrap();
        stmt.query_map([], |r| r.get::<_, String>(0))
            .unwrap()
            .map(|r| r.unwrap())
            .collect()
    };
    assert_eq!(
        remaining.len(),
        3,
        "在场消息 blob + 段成员 blob + 引用列表 blob"
    );
    // 段引用可解析（成员 blob 未被清扫）
    let seg = store
        .get_message_segment("seg_keep", "default")
        .await
        .unwrap()
        .expect("段行在");
    assert_eq!(
        seg.members.as_ref().unwrap()[0]["content"],
        "被删后缀",
        "槽位删除后段成员全文仍可达"
    );
}

/// 级联删除：删管道连带清 message_segments 段行；其它管道段行不受波及。
#[tokio::test]
async fn delete_pipeline_cascades_message_segments() {
    let store = SqliteStore::open_memory().unwrap();
    let freeze = |id: &str| {
        json!({
            "op": "freeze_segment", "id": id,
            "base_seq": 0, "base_len": 1, "visible_to": "",
            "preview": "p", "members": [{"role": "user", "content": "x"}],
        })
    };
    store
        .apply_messages_ops_to_table("pipe_del", "default", &[freeze("seg_del")])
        .unwrap();
    store
        .apply_messages_ops_to_table("pipe_keep", "default", &[freeze("seg_keep")])
        .unwrap();

    store.delete_pipeline("pipe_del").await.unwrap();

    assert_eq!(
        store
            .conn
            .lock()
            .query_row(
                "SELECT COUNT(*) FROM message_segments WHERE pipeline_id = 'pipe_del'",
                [],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
        0,
        "被删管道的段行级联清理"
    );
    assert_eq!(
        store
            .conn
            .lock()
            .query_row(
                "SELECT COUNT(*) FROM message_segments WHERE pipeline_id = 'pipe_keep'",
                [],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
        1,
        "其它管道段行不受波及"
    );
}
