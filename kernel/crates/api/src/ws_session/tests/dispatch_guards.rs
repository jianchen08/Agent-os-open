use super::*;

use agentos_session::EventSink;

use agentos_session::router::InboundRouter;

use super::support::{seed_three_slot_history, CapturingSink, ErrUserInputDispatcher};

use super::super::{emit_plugin_error_events, handle_inbound, SessionRoundEvents};

// ── thread 归属校验（M1）：映射注册/回放门禁 ──
// thread_id 完全来自客户端帧；thread→user 映射是事件单播与断线回放的
// 唯一路由依据，不校验即任意已认证连接可借他人 thread_id 劫持事件流。

#[tokio::test]
async fn thread_ownership_gate_allows_owned_thread() {
    // 请求者租户下有管道关联 → 放行（含回放资格）
    let mock = ResolveMock::new(Ok(vec!["P-1".to_string()]), None);
    let store = Arc::new(mock) as Arc<dyn StorageBackend>;
    assert!(
        thread_owned_by_user(Some(&store), "T1", "u1").await,
        "本租户 thread 必须放行"
    );
}

#[tokio::test]
async fn thread_ownership_gate_rejects_unlinked_and_foreign_thread() {
    // 本租户无关联（外来 thread 或无关联新会话）→ 拒绝：注册/回放都不发生
    let mock = ResolveMock::new(Ok(vec![]), None);
    let store = Arc::new(mock) as Arc<dyn StorageBackend>;
    assert!(
        !thread_owned_by_user(Some(&store), "T1", "u1").await,
        "无管道关联的 thread 一律拒绝"
    );
}

#[tokio::test]
async fn thread_ownership_gate_fails_closed_on_storage_error() {
    // 存储故障 ≠ 无关联的放行理由：归属不可证即不注册（fail-closed）
    let mock = ResolveMock::new(
        Err(StorageError::NotFound("mock query failure".to_string())),
        None,
    );
    let store = Arc::new(mock) as Arc<dyn StorageBackend>;
    assert!(
        !thread_owned_by_user(Some(&store), "T1", "u1").await,
        "查询失败必须拒绝注册"
    );
}

#[tokio::test]
async fn thread_ownership_gate_allows_missing_store_degraded_mode() {
    // 无 store（单测/嵌入式降级模式）：无解析依据，维持旧行为
    assert!(thread_owned_by_user(None, "T1", "u1").await);
}

// ── 租户解析失败拒分发（K8 修订 ADR 2026-10-10）──
//
// 旧路径 Err/None 合流后降级 default 继续分发 = 数据写串租户；
// 新契约：NotFound 直接拒绝，StoreError fail-closed（带坐标回执 +
// 4001 族关闭），Found 返回真实租户。

/// StoreError → fail-closed：dispatch 拒绝；带坐标的 user_input_ack
/// (ok:false) 先行送达（关闭后常规错误帧会丢失，回执契约 ADR
/// 2026-09-29）；连接收 4001 族关闭信号（CLOSE_AUTH_REJECT）。
#[tokio::test]
async fn dispatch_store_error_fails_closed_with_ack_then_auth_reject_close() {
    let mock = ResolveMock::new(Ok(vec!["P-1".to_string()]), None);
    *mock.user_lookup.lock().unwrap() = Some(Err(StorageError::Database(
        "injected user lookup outage".to_string(),
    )));
    let store: Arc<dyn StorageBackend> = Arc::new(mock);
    let coord = Arc::new(agentos_session::SessionCoordinator::new());
    let (sink, mut out_rx, close_rx) = WsSink::new();
    coord.register("u1", sink);
    let mut state = crate::routes::AppState::new();
    state.store = Some(store.clone());
    state.session = Some(coord);
    let dispatcher = EngineDispatcher::new(state);

    let err = dispatcher
        .dispatch_user_input(
            "T1",
            "u1",
            "hi",
            "P-1",
            "",
            None,
            None,
            "",
            None,
            "cm-err",
            PendingInputSource::User,
        )
        .await
        .unwrap_err();
    assert!(
        err.contains("fail-closed") && err.contains("injected user lookup outage"),
        "拒绝原因必须归因存储故障: {err}"
    );

    // 带坐标回执先行入队（先于关闭信号置位，排空任务先清队列再 Close）
    let ack_text = out_rx.try_recv().expect("StoreError 拒绝必须回带坐标 ack");
    let ack: serde_json::Value = serde_json::from_str(&ack_text).unwrap();
    assert_eq!(ack["type"], "user_input_ack");
    assert_eq!(ack["client_message_id"], "cm-err");
    assert_eq!(ack["thread_id"], "T1");
    assert_eq!(ack["ok"], false, "回执必须 ok:false");
    // 4001 族关闭信号已置位
    assert_eq!(*close_rx.borrow(), CLOSE_AUTH_REJECT);
    // fail-closed 性质断言：拒绝路径不得写任何租户的 pending 队列
    assert!(
        store
            .list_pending_inputs("default", "P-1")
            .await
            .unwrap()
            .is_empty(),
        "拒绝分发不得留队列残留"
    );
}

/// NotFound（已删用户）→ 直接拒绝（拍板①）：dispatch Err，错误串归因
/// 用户不存在；不发关闭信号（刷新链路自愈，避免瞬态误关全部连接）。
#[tokio::test]
async fn dispatch_deleted_user_rejected_without_close() {
    let mock = ResolveMock::new(Ok(vec!["P-1".to_string()]), None);
    *mock.user_lookup.lock().unwrap() = Some(Ok(None));
    let store: Arc<dyn StorageBackend> = Arc::new(mock);
    let coord = Arc::new(agentos_session::SessionCoordinator::new());
    let (sink, _out_rx, close_rx) = WsSink::new();
    coord.register("u1", sink);
    let mut state = crate::routes::AppState::new();
    state.store = Some(store);
    state.session = Some(coord);
    let dispatcher = EngineDispatcher::new(state);

    let err = dispatcher
        .dispatch_user_input(
            "T1",
            "u1",
            "hi",
            "P-1",
            "",
            None,
            None,
            "",
            None,
            "cm-del",
            PendingInputSource::User,
        )
        .await
        .unwrap_err();
    assert!(err.contains("不存在"), "拒绝原因必须归因用户不存在: {err}");
    assert_eq!(
        *close_rx.borrow(),
        CLOSE_NONE,
        "NotFound 拒绝不发关闭信号（非 fail-closed 类）"
    );
}

/// tenant=B 动态用户（整改方案 §2-D1 第一刀/§0 待实证疑点收口）：
/// WS dispatch 解析出真实租户 B 落到管道坐标解析，不落 default 降级。
/// 真实后端 get_user_by_id 的 tenant-unscoped 语义由 engine 侧
/// store_pool_trait_methods 锁定测试保障。
#[tokio::test]
async fn dispatch_resolves_non_default_tenant_for_dynamic_user() {
    let mock = Arc::new(ResolveMock::new(Ok(vec!["P-1".to_string()]), None));
    let mut u = synthetic_default_user("u-b");
    u.tenant_id = "tenant-b".to_string();
    *mock.user_lookup.lock().unwrap() = Some(Ok(Some(u)));
    let store: Arc<dyn StorageBackend> = mock.clone();
    let mut state = crate::routes::AppState::new();
    state.store = Some(store);
    let dispatcher = EngineDispatcher::new(state);

    dispatcher
        .dispatch_user_input(
            "T1",
            "u-b",
            "hi",
            "P-1",
            "",
            None,
            None,
            "",
            None,
            "cm-b",
            PendingInputSource::User,
        )
        .await
        .unwrap();
    let args = mock.seen_tenant_args.lock().unwrap().clone();
    assert!(
        !args.is_empty() && args.iter().all(|t| t == "tenant-b"),
        "管道坐标解析必须全程使用真实租户 tenant-b（不得降级 default）: {args:?}"
    );
}

#[tokio::test]
async fn outcome_waiter_remove_prevents_late_notify() {
    // 超时自清理语义：remove 删除条目（sender 随之 drop）后，迟到的
    // notify 不得送达任何 outcome——等待侧已以超时错误收尾，先到先得。
    let (tx, mut rx) = tokio::sync::oneshot::channel::<crate::server::EngineOutcome>();
    let cmid = "http_remove_probe_1".to_string();
    register_outcome_waiter(cmid.clone(), tx);
    remove_outcome_waiter(&cmid);
    notify_outcome_waiter(
        &cmid,
        crate::server::EngineOutcome {
            content: "late".to_string(),
            final_assistant: None,
            failed: true,
            degraded: false,
            plugin_errors: Vec::new(),
            consumed_pending_cmids: Vec::new(),
        },
    );
    match rx.try_recv() {
        Ok(outcome) => panic!("迟到通知不得送达，实际收到: {}", outcome.content),
        Err(tokio::sync::oneshot::error::TryRecvError::Closed) => {
            // sender 已随条目删除被 drop，"late" 从未送达 —— 语义正确
        }
        Err(tokio::sync::oneshot::error::TryRecvError::Empty) => {
            panic!("条目已删但 sender 存活，删除语义不成立")
        }
    }
}

#[test]
fn message_status_reads_from_blob() {
    // blob 带 status（中断/错误半截消息）→ 原样透传
    let interrupted =
        serde_json::json!({"role": "assistant", "content": "半截", "status": "interrupted"});
    assert_eq!(
        message_status_from_blob(&interrupted),
        serde_json::json!("interrupted")
    );
    let errored = serde_json::json!({"role": "assistant", "content": "x", "status": "error"});
    assert_eq!(
        message_status_from_blob(&errored),
        serde_json::json!("error")
    );
}

#[test]
fn message_status_defaults_to_completed_without_blob_status() {
    // 正常消息 blob 无 status → completed（既有行为不变）
    let plain = serde_json::json!({"role": "assistant", "content": "ok"});
    assert_eq!(
        message_status_from_blob(&plain),
        serde_json::json!("completed")
    );
}

// ── dispatch_stop：停止 = 复用 suspend_pipeline 落库路径（批次 C）──
// 传输信号（Suspended → llm_core 轮询感知中断），不是终态。

fn stop_state(store: Arc<dyn StorageBackend>) -> EngineDispatcher {
    let mut state = crate::routes::AppState::new();
    state.store = Some(store);
    EngineDispatcher::new(state)
}

async fn stop_running(store: Arc<dyn StorageBackend>, thread_id: &str) -> Result<(), String> {
    stop_state(store).dispatch_stop(thread_id, "").await
}

async fn stop_pipeline(
    store: Arc<dyn StorageBackend>,
    thread_id: &str,
    pipeline_id: &str,
) -> Result<(), String> {
    stop_state(store)
        .dispatch_stop(thread_id, pipeline_id)
        .await
}

#[tokio::test]
async fn dispatch_stop_suspends_latest_running_run() {
    // 有 running run：按 thread 主管道定位并置 Suspended（传输信号）。
    let store =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap()) as Arc<dyn StorageBackend>;
    store
        .record_run_start("p1", "default", "run-1", "cfg", "2026-10-15T00:00:00+00:00")
        .await
        .unwrap();
    store
        .create_session(&session_record(Some("p1")))
        .await
        .unwrap();

    stop_running(store.clone(), "T1").await.unwrap();

    let run = store.get_run("run-1").await.unwrap();
    assert_eq!(
        run.status,
        RunStatus::Suspended,
        "stop 应把最新 running run 置 suspended"
    );
}

#[tokio::test]
async fn dispatch_stop_no_running_run_is_idempotent_noop() {
    // run 已完成才点停止（竞态）：无 Running run → 幂等空转，不报错、不动已完成 run。
    let store =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap()) as Arc<dyn StorageBackend>;
    store
        .record_run_start("p1", "default", "run-1", "cfg", "2026-10-15T00:00:00+00:00")
        .await
        .unwrap();
    store
        .upsert_state_fields(
            "p1",
            "default",
            &json!({ "run_status": "completed" })
                .as_object()
                .unwrap()
                .clone(),
        )
        .await
        .unwrap();
    store
        .create_session(&session_record(Some("p1")))
        .await
        .unwrap();

    stop_running(store.clone(), "T1").await.unwrap();

    let run = store.get_run("run-1").await.unwrap();
    assert_eq!(
        run.status,
        RunStatus::Completed,
        "已完成 run 不被 stop 改写"
    );
    let state_fields = store.load_pipeline_state("p1", "default").await.unwrap();
    assert_ne!(
        state_fields.get("pending_cancel").and_then(|v| v.as_bool()),
        Some(true),
        "无在途派发（链空闲）点停：不落待取消标记（不给下一轮设取消陷阱）"
    );
}

#[tokio::test]
async fn dispatch_stop_before_run_established_lands_pending_cancel_marker() {
    // 首 token 前空窗（缺陷A，台账 R1 ④immediate）：stop_generation 在 run
    // 建立前到达——无 Running run 可挂起，但派发链在途（user_input 已受理、
    // 引擎 run 尚未建行）。取消必须生效：落管道级待取消标记，run 创建起手
    // 由引擎消费（禁静默吞）。
    let store =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap()) as Arc<dyn StorageBackend>;
    store
        .create_session(&session_record(Some("p1")))
        .await
        .unwrap();
    // 派发链在途：占链（fut 挂起等释放）模拟「user_input 已受理、run 未建行」。
    // 管道 id 带测试专名，避免污染并行测试共享的进程级链注册表。
    let registry = crate::run_chain::RunChainRegistry::global();
    let (release_tx, release_rx) = tokio::sync::oneshot::channel::<()>();
    registry.try_enqueue_direct("p1-pending-cancel", "u1", async move {
        let _ = release_rx.await;
    });
    // 主管道定位到占链管道（会话 active_pipeline_id 需一致）。
    store
        .update_session(&SessionRecord {
            active_pipeline_id: Some("p1-pending-cancel".to_string()),
            ..session_record(Some("p1"))
        })
        .await
        .unwrap();

    stop_running(store.clone(), "T1").await.unwrap();

    let state_fields = store
        .load_pipeline_state("p1-pending-cancel", "default")
        .await
        .unwrap();
    assert_eq!(
        state_fields.get("pending_cancel").and_then(|v| v.as_bool()),
        Some(true),
        "run 建立前点停：待取消标记落管道 state（引擎 run 起手消费）"
    );
    let _ = release_tx.send(());
}

#[tokio::test]
async fn dispatch_stop_with_frontend_pipeline_id_targets_that_pipeline() {
    // 前端携带 pipeline_id（正在查看的子任务管道）→ 停该管道的 run，
    // 不得因「不属于 thread 成员」被改写回主管道（历史 bug：点停止
    // 永远停不住子任务管道）。
    let store =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap()) as Arc<dyn StorageBackend>;
    // 主管道 p-main 有 running run；子任务管道 p-sub 也有 running run。
    store
        .record_run_start(
            "p-main",
            "default",
            "run-main",
            "cfg",
            "2026-10-15T00:00:00+00:00",
        )
        .await
        .unwrap();
    store
        .record_run_start(
            "p-sub",
            "default",
            "run-sub",
            "cfg",
            "2026-10-15T00:00:00+00:00",
        )
        .await
        .unwrap();
    store
        .create_session(&session_record(Some("p-main")))
        .await
        .unwrap();

    stop_pipeline(store.clone(), "T1", "p-sub").await.unwrap();

    let main = store.get_run("run-main").await.unwrap();
    let sub = store.get_run("run-sub").await.unwrap();
    assert_eq!(
        sub.status,
        RunStatus::Suspended,
        "前端指定的子任务管道 run 被停止"
    );
    assert_eq!(
        main.status,
        RunStatus::Running,
        "主管道 run 不受影响（停止目标跟随前端管道 ID）"
    );
}

// ── 消息段：regenerate 冻结 + segment_activate（多代切换）──

/// regenerate 截断前把被换后缀冻结为段（同批 ops 同事务）：段统一含目标
/// user 消息（[target_seq..末尾]，edit-resend 改写目标槽位后旧用户文本仍在
/// 段里），成员为槽位全文，rollback trace 的 messages._ops 携带 freeze op 全文。
#[tokio::test]
async fn regenerate_freezes_deleted_suffix_segment() {
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    seed_three_slot_history(&store, "regGd1").await;
    let user_id = store
        .get_messages_by_pipeline("regGd1", MessageQueryOpts::default())
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
        .dispatch_regenerate("u1", "T1", "regGd1", &user_id, None)
        .await
        .unwrap();

    // 被换后缀（seq 0..2 整段，含目标 user）冻结为段：visible_to=''、
    // preview 取首条、成员全文
    let segments = store
        .list_message_segments("regGd1", "default")
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
    seed_three_slot_history(&store, "regGd2").await;
    let user_id = store
        .get_messages_by_pipeline("regGd2", MessageQueryOpts::default())
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
        .dispatch_regenerate("u1", "T1", "regGd2", &user_id, Some("改写后的问题"))
        .await
        .unwrap();

    // 目标槽位已原地改写，旧文本只在冻结段里
    let rows = store
        .get_messages_by_pipeline("regGd2", MessageQueryOpts::default())
        .await
        .unwrap();
    assert_eq!(rows[0].content_preview.as_deref(), Some("改写后的问题"));
    let seg = store
        .list_message_segments("regGd2", "default")
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
            "regGd2",
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
        .dispatch_segment_activate("u1", "T1", "regGd2", &seg.id)
        .await
        .unwrap();

    let rows = sqlite
        .get_slot_messages_by_pipeline("regGd2", "default", MessageQueryOpts::default())
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

// ── resolve_dispatch_agent：执行 agent 解析 ──
// 优先级：显式传入（任务派发）→ registry 线程绑定（热）→ 主管道 state
// agent.id（冷，绑定真值）→ agentos。sessions.agent_id 已降为投影（F2
// 收敛），不作绑定读源。

fn session_with_agent(agent_id: Option<&str>) -> SessionRecord {
    let mut s = session_record(Some("P-main"));
    s.agent_id = agent_id.map(|a| a.to_string());
    s
}

async fn resolve_agent(
    registry: Option<&agentos_session::ConnectionRegistry>,
    mock: Option<ResolveMock>,
    explicit: &str,
) -> String {
    let store = mock.map(|m| Arc::new(m) as Arc<dyn StorageBackend>);
    resolve_dispatch_agent(registry, store.as_ref(), "default", "T1", explicit).await
}

#[tokio::test]
async fn dispatch_agent_explicit_wins_over_binding() {
    // 任务派发显式传 agent（非空）→ 不被线程绑定覆盖（chat.send_message 路径语义）
    let registry = agentos_session::ConnectionRegistry::new();
    registry.register_thread_agent("T1", "general_agent");
    let mock = ResolveMock::new(Ok(vec![]), Some(session_with_agent(Some("general_agent"))));
    assert_eq!(
        resolve_agent(Some(&registry), Some(mock), "code_writer_agent").await,
        "code_writer_agent"
    );
}

#[tokio::test]
async fn dispatch_agent_hot_registry_binding_used() {
    // 热路径：registry 线程绑定优先于冷读（会话编辑后即生效）
    let registry = agentos_session::ConnectionRegistry::new();
    registry.register_thread_agent("T1", "general_agent");
    let mock = ResolveMock::new(Ok(vec![]), Some(session_with_agent(Some("agentos"))));
    assert_eq!(
        resolve_agent(Some(&registry), Some(mock), "").await,
        "general_agent"
    );
}

#[tokio::test]
async fn dispatch_agent_cold_reads_state_agent_id_single_source() {
    // 冷路径（registry 无绑定，如内核重启后）：单源读主管道 state agent.id。
    // sessions.agent_id 残留旧投影值（stale_agent）不得被读到——读投影会
    // 复活双写漂移通道（F2）。
    let registry = agentos_session::ConnectionRegistry::new();
    let mock = ResolveMock::new(Ok(vec![]), Some(session_with_agent(Some("stale_agent"))));
    mock.set_state_fields(json!({"agent.id": "general_agent"}));
    assert_eq!(
        resolve_agent(Some(&registry), Some(mock), "").await,
        "general_agent",
        "冷读只认 state 真值，不认 sessions 投影"
    );
}

#[tokio::test]
async fn dispatch_agent_state_binding_missing_falls_to_agentos() {
    // state 无 agent.id（如出生绑定未落的遗留会话）→ 兜底主 agent；
    // sessions.agent_id 即使有值也不作回退源（单源裁决）。
    let registry = agentos_session::ConnectionRegistry::new();
    let with_projection =
        ResolveMock::new(Ok(vec![]), Some(session_with_agent(Some("general_agent"))));
    assert_eq!(
        resolve_agent(Some(&registry), Some(with_projection), "").await,
        "agentos",
        "投影值不得充当绑定读源"
    );
    let empty_agent = ResolveMock::new(Ok(vec![]), Some(session_with_agent(Some(""))));
    assert_eq!(
        resolve_agent(Some(&registry), Some(empty_agent), "").await,
        "agentos"
    );
}

#[tokio::test]
async fn dispatch_agent_no_registry_no_db_defaults_agentos() {
    let mock = ResolveMock::new(Ok(vec![]), None);
    assert_eq!(resolve_agent(None, Some(mock), "").await, "agentos");
}

// ── assistant 权威 seq 提取（扫描 2026-08-27 跨辖区 #20 契约对齐）──

#[test]
fn assistant_seq_missing_yields_null_not_fake_one() {
    // 契约 = assistant 元素缺 seq 时挂 null（不补位、不伪造默认序号）。
    let no_seq = serde_json::json!({"role": "assistant", "content": "x"});
    assert!(
        assistant_authoritative_seq(Some(&no_seq)).is_null(),
        "元素缺 seq 必须发 null，不得伪造默认序号"
    );
    assert!(
        assistant_authoritative_seq(None).is_null(),
        "无 assistant 消息同样 null"
    );
}

#[test]
fn assistant_seq_passes_through_authoritative_value_identity() {
    // 直传恒等性质：有 seq 时输出值与输入完全一致（多量级采样）
    for seq in [1u64, 7, 4_000_000_000_000] {
        let fa = serde_json::json!({"role": "assistant", "seq": seq});
        assert_eq!(
            assistant_authoritative_seq(Some(&fa)),
            serde_json::json!(seq),
            "带 seq 元素必须原值直传"
        );
        let other = serde_json::json!({"role": "assistant", "seq": seq + 1});
        assert_ne!(
            assistant_authoritative_seq(Some(&fa)),
            assistant_authoritative_seq(Some(&other)),
            "不同 seq 输入产生不同输出（非常量映射）"
        );
    }
}

/// run 级收尾事件帧：事件名/pipeline_id 路由键/failed 标志逐字段锁定
/// （前端生成态终止信号——字段缺失或改名即前端终止断链）。
#[tokio::test]
async fn round_finished_event_frame_shape() {
    struct CaptureSink {
        frames: std::sync::Arc<Mutex<Vec<String>>>,
    }
    #[async_trait::async_trait]
    impl agentos_session::EventSink for CaptureSink {
        async fn send_text(&self, text: &str) -> bool {
            self.frames.lock().unwrap().push(text.to_string());
            true
        }
        fn id(&self) -> u64 {
            7
        }
    }
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    let coord = agentos_session::SessionCoordinator::new();
    coord.register(
        "u1",
        Arc::new(CaptureSink {
            frames: frames.clone(),
        }),
    );
    coord.register_thread("thread-rf", "u1");

    // 事件信封带 _threadId 时按 thread 单播（生产路径：emit_event thread 坐标）
    emit_round_finished_event(&coord, "thread-rf", "pipe-rf-1", false, false, 0).await;
    let captured = frames.lock().unwrap();
    assert_eq!(captured.len(), 1, "应恰好一帧");
    let parsed: serde_json::Value = serde_json::from_str(&captured[0]).unwrap();
    assert_eq!(parsed["type"], "pipeline_round_finished");
    assert_eq!(parsed["data"]["pipeline_id"], "pipe-rf-1");
    assert_eq!(parsed["data"]["failed"], false);
    assert_eq!(parsed["data"]["cancelled"], false);
    assert_eq!(parsed["data"]["queue_depth"], 0);
}

/// failed=true 透传：失败路径（stream_error 先行终止后）的 run 收尾帧
/// 必须携带 failed=true，前端据此把注册表状态标 failed 而非 completed。
#[tokio::test]
async fn round_finished_event_failed_flag_passthrough() {
    struct CaptureSink {
        frames: std::sync::Arc<Mutex<Vec<String>>>,
    }
    #[async_trait::async_trait]
    impl agentos_session::EventSink for CaptureSink {
        async fn send_text(&self, text: &str) -> bool {
            self.frames.lock().unwrap().push(text.to_string());
            true
        }
        fn id(&self) -> u64 {
            8
        }
    }
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    let coord = agentos_session::SessionCoordinator::new();
    coord.register(
        "u1",
        Arc::new(CaptureSink {
            frames: frames.clone(),
        }),
    );
    coord.register_thread("thread-rf-fail", "u1");

    emit_round_finished_event(&coord, "thread-rf-fail", "pipe-rf-2", true, true, 3).await;
    let captured = frames.lock().unwrap();
    let parsed: serde_json::Value = serde_json::from_str(&captured[0]).unwrap();
    assert_eq!(parsed["data"]["failed"], true);
    assert_eq!(parsed["data"]["cancelled"], true);
    assert_eq!(parsed["data"]["queue_depth"], 3);
}

/// 出站队列有界：容量内投递成功；超容 → send_text=false（registry 据此
/// 注销连接）+ 背压关闭信号；shutdown（超限踢旧）在队列满时仍可达
/// （watch 不依赖队列容量）。
#[tokio::test]
async fn ws_sink_bounded_queue_full_signals_backpressure_and_shutdown_reachable() {
    let (sink, mut _rx, mut close_rx) = WsSink::new();
    for i in 0..WS_OUTBOUND_CAPACITY {
        assert!(
            sink.send_text(&format!("f{i}")).await,
            "容量内第 {i} 帧投递应成功"
        );
    }
    assert_eq!(*close_rx.borrow_and_update(), CLOSE_NONE);

    assert!(
        !sink.send_text("overflow").await,
        "满载应返回 false（registry 既有语义：注销该连接）"
    );
    assert_eq!(
        *close_rx.borrow_and_update(),
        CLOSE_BACKPRESSURE,
        "满载触发背压关闭信号（排空任务发普通 Close，前端自动重连自愈）"
    );

    sink.shutdown();
    assert_eq!(
        *close_rx.borrow_and_update(),
        CLOSE_KICKED,
        "队列满时 shutdown 仍可达（watch 与帧队列解耦）"
    );
}

/// 鉴权类拒绝关闭信号（K8 修订 ADR 2026-10-10）：shutdown_auth_reject
/// 置 CLOSE_AUTH_REJECT（区别于踢旧/背压），队列满时仍可达；已入队帧
/// 不被丢弃（排空任务先清队列再发 Close——拒绝对象是连接不是在途回执）。
#[tokio::test]
async fn ws_sink_auth_reject_signal_reachable_and_keeps_queued_frames() {
    let (sink, mut out_rx, mut close_rx) = WsSink::new();
    assert_eq!(*close_rx.borrow_and_update(), CLOSE_NONE);

    // 回执帧先入队（拒绝前置回执路径），关闭信号后置——信号不消费队列
    assert!(sink.send_text("user_input_ack").await);
    sink.shutdown_auth_reject();
    assert_eq!(
        *close_rx.borrow_and_update(),
        CLOSE_AUTH_REJECT,
        "鉴权类拒绝置 4001 族关闭信号（区别于踢旧 CLOSE_KICKED/背压）"
    );
    assert_eq!(
        out_rx.try_recv().unwrap(),
        "user_input_ack",
        "关闭信号不清空帧队列（排空任务先发帧再 Close，序由构造保证）"
    );

    // 队列满载时关闭信号同样可达（watch 与帧队列解耦，同 shutdown 语义）
    let (sink_full, _rx_full, mut close_full) = WsSink::new();
    for i in 0..WS_OUTBOUND_CAPACITY {
        assert!(sink_full.send_text(&format!("f{i}")).await);
    }
    sink_full.shutdown_auth_reject();
    assert_eq!(*close_full.borrow_and_update(), CLOSE_AUTH_REJECT);
}

// ── 补测：WsSink 关闭通道 / outcome 等待桥命中路径 ─────────────────────

use agentos_core::traits::PluginManifest;

fn manifest_base(plugin_id: &str) -> PluginManifest {
    PluginManifest {
        force_include_tools: Vec::new(),
        state: None,
        id: plugin_id.to_string(),
        name: plugin_id.to_string(),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: agentos_core::traits::PluginType::System,
        language: "python".to_string(),
        host_type: agentos_core::traits::HostType::Sidecar,
        host_group: None,
        entry: "python server.py".to_string(),
        capabilities: Default::default(),
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
    }
}

#[tokio::test]
async fn ws_sink_send_text_returns_false_after_receiver_dropped() {
    // 接收端已 drop（排空任务结束）→ try_send Closed → false（连接判死）。
    let (sink, rx, mut close_rx) = WsSink::new();
    drop(rx);
    assert!(
        !sink.send_text("after-close").await,
        "通道已关必须返回 false 而非静默丢帧"
    );
    assert_eq!(
        *close_rx.borrow_and_update(),
        CLOSE_NONE,
        "Closed 分支不触发背压关闭信号（连接已死，无需自愈）"
    );
}

#[tokio::test]
async fn notify_outcome_waiter_delivers_outcome_on_match() {
    // 消费循环一轮结束按 cmid 命中等待者：outcome 送达、条目移除。
    let (tx, mut rx) = tokio::sync::oneshot::channel::<crate::server::EngineOutcome>();
    let cmid = "http_notify_probe_1".to_string();
    register_outcome_waiter(cmid.clone(), tx);
    notify_outcome_waiter(
        &cmid,
        crate::server::EngineOutcome {
            content: "回复内容".to_string(),
            final_assistant: Some(json!({"role": "assistant", "content": "回复内容"})),
            failed: false,
            degraded: false,
            plugin_errors: Vec::new(),
            consumed_pending_cmids: Vec::new(),
        },
    );
    let outcome = rx.try_recv().unwrap();
    assert_eq!(outcome.content, "回复内容");
    assert!(!outcome.failed);
    // 通知即移除：二次通知静默 no-op，不得重复送达
    notify_outcome_waiter(
        &cmid,
        crate::server::EngineOutcome {
            content: "late".to_string(),
            final_assistant: None,
            failed: false,
            degraded: false,
            plugin_errors: Vec::new(),
            consumed_pending_cmids: Vec::new(),
        },
    );
    assert!(rx.try_recv().is_err(), "条目已移除，迟到通知不得送达");
}

// ── 补测：plugin_error 事件出口（插件失败可见性）──────────────────────

#[tokio::test]
async fn plugin_error_events_emit_fields_and_defaults() {
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    let coord = agentos_session::SessionCoordinator::new();
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    coord.register_thread("thread-pe", "u1");

    let outcome = crate::server::EngineOutcome {
        content: String::new(),
        final_assistant: None,
        failed: false,
        degraded: false,
        plugin_errors: vec![
            json!({"plugin_id": "p1", "code": "TIMEOUT", "message": "超时"}),
            json!({}), // 缺失字段全部走缺省
        ],
        consumed_pending_cmids: Vec::new(),
    };
    emit_plugin_error_events(&coord, "thread-pe", "pipe-pe", "a_pe", &outcome).await;

    let captured = frames.lock().unwrap();
    assert_eq!(captured.len(), 2, "每个插件错误恰好一帧");
    let first: serde_json::Value = serde_json::from_str(&captured[0]).unwrap();
    assert_eq!(first["type"], "plugin_error");
    assert_eq!(first["data"]["plugin_id"], "p1");
    assert_eq!(first["data"]["error"]["code"], "TIMEOUT");
    assert_eq!(first["data"]["error"]["message"], "超时");
    assert_eq!(first["data"]["error"]["source"], "plugin");
    assert_eq!(first["data"]["error"]["retryable"], false);

    let second: serde_json::Value = serde_json::from_str(&captured[1]).unwrap();
    assert_eq!(
        second["data"]["plugin_id"], "unknown",
        "缺 plugin_id 用 unknown"
    );
    assert_eq!(
        second["data"]["error"]["code"], "PLUGIN_EXEC_FAILED",
        "缺 code 用缺省（插件失败重跑无意义）"
    );
    assert_eq!(second["data"]["error"]["message"], "插件执行失败");
}

// ── 补测：SessionRoundEvents 轮次事件桥接（一轮 = 一条消息）──────────

/// SessionRoundEvents 消费事件发射面的 store 依赖：ResolveMock 空列表形态
/// （list_pending_inputs 默认返回空，事件 payload items=[]）。
fn test_store() -> Arc<dyn StorageBackend> {
    Arc::new(super::ResolveMock::new(Ok(vec![]), None)) as Arc<dyn StorageBackend>
}

pub(super) async fn frame_types(
    frames: &Arc<Mutex<Vec<String>>>,
) -> Vec<(String, serde_json::Value)> {
    frames
        .lock()
        .unwrap()
        .iter()
        .filter_map(|f| {
            serde_json::from_str::<serde_json::Value>(f)
                .ok()
                .map(|v| (v["type"].as_str().unwrap_or("").to_string(), v))
        })
        .collect()
}

#[tokio::test]
async fn session_round_events_full_flow_claims_user_once() {
    let coord = Arc::new(agentos_session::SessionCoordinator::new());
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let events = SessionRoundEvents::new(
        coord.clone(),
        "thread-sr",
        "pipe-sr",
        "u1",
        test_store(),
        "default",
    );

    // 轮开始：stream_start + thread/route 双注册（事件单播坐标）
    agentos_engine::RoundEvents::on_round_start(
        &events,
        agentos_engine::RoundStart {
            round_index: 1,
            message_id: "a_1".into(),
            sequence: 5,
            pipeline_id: "pipe-sr".into(),
            thread_id: "thread-sr".into(),
        },
    )
    .await;
    let registry = coord.registry();
    assert_eq!(
        registry.get_user_for_thread("thread-sr").as_deref(),
        Some("u1"),
        "thread 坐标注册（thread→user 反查索引）"
    );
    assert_eq!(
        registry.get_user_for_thread("pipe-sr").as_deref(),
        Some("u1"),
        "route 坐标同样注册（触发器注入路径按 pipeline_id 寻址）"
    );

    // 首个有产出轮次：new_message（含 cmid 认领回传 + user_record）+ stream_end
    agentos_engine::RoundEvents::on_round_end(
        &events,
        agentos_engine::RoundEnd {
            round_index: 1,
            message_id: "a_1".into(),
            pipeline_id: "pipe-sr".into(),
            thread_id: "thread-sr".into(),
            assistant: Some(json!({"role": "assistant", "content": "答", "seq": 5})),
            user_message: Some(json!({
                "role": "user", "content": "问", "seq": 4,
                "metadata": {"client_message_id": "cm-9"}
            })),
        },
    )
    .await;
    {
        let all = frame_types(&frames).await;
        let new_msg = all
            .iter()
            .find(|(t, _)| t == "new_message")
            .map(|(_, v)| v.clone())
            .expect("首轮应有 new_message");
        assert_eq!(new_msg["data"]["sequence"], 5, "权威 seq 直传");
        assert_eq!(new_msg["data"]["message"]["role"], "assistant");
        assert_eq!(new_msg["data"]["message"]["sequence"], 5);
        assert_eq!(
            new_msg["data"]["message"]["status"], "completed",
            "正常消息缺省 completed"
        );
        assert_eq!(
            new_msg["data"]["client_message_id"], "cm-9",
            "cmid 幂等键随首个有产出轮次回传"
        );
        assert_eq!(new_msg["data"]["user_message"]["content"], "问");
        assert_eq!(new_msg["data"]["user_message"]["sequence"], 4);
        assert!(
            new_msg["data"]["user_message"]["id"]
                .as_str()
                .is_some_and(|s| !s.is_empty()),
            "user_record 指纹 id 非空（前端认领键）"
        );
        let end = all
            .iter()
            .find(|(t, _)| t == "stream_end")
            .map(|(_, v)| v.clone())
            .expect("应有 stream_end");
        assert_eq!(end["data"]["final_sequence"], 5);
    }

    // 第二轮：user 认领不重复携带（幂等键二次认领多余）
    frames.lock().unwrap().clear();
    agentos_engine::RoundEvents::on_round_end(
        &events,
        agentos_engine::RoundEnd {
            round_index: 2,
            message_id: "a_2".into(),
            pipeline_id: "pipe-sr".into(),
            thread_id: "thread-sr".into(),
            assistant: Some(json!({"role": "assistant", "content": "答2", "seq": 7})),
            user_message: Some(json!({"role": "user", "content": "问2", "seq": 6})),
        },
    )
    .await;
    {
        let all = frame_types(&frames).await;
        let new_msg = all
            .iter()
            .find(|(t, _)| t == "new_message")
            .map(|(_, v)| v.clone())
            .expect("第二轮仍有 new_message");
        assert!(
            new_msg["data"].get("client_message_id").is_none(),
            "认领回传仅首个有产出轮次携带: {new_msg}"
        );
        assert!(
            new_msg["data"].get("user_message").is_none(),
            "user_record 不重复携带"
        );
    }

    // 无产出轮次：仅 stream_end（不伪造 new_message、不携带认领）
    frames.lock().unwrap().clear();
    agentos_engine::RoundEvents::on_round_end(
        &events,
        agentos_engine::RoundEnd {
            round_index: 3,
            message_id: "a_3".into(),
            pipeline_id: "pipe-sr".into(),
            thread_id: "thread-sr".into(),
            assistant: None,
            user_message: Some(json!({"role": "user", "content": "问3"})),
        },
    )
    .await;
    {
        let all = frame_types(&frames).await;
        assert!(
            !all.iter().any(|(t, _)| t == "new_message"),
            "无 assistant 产出轮次不得发 new_message"
        );
        let end = all
            .iter()
            .find(|(t, _)| t == "stream_end")
            .map(|(_, v)| v.clone())
            .expect("无产出轮次仍要收尾 stream_end");
        assert_eq!(end["data"]["final_sequence"], serde_json::Value::Null);
    }
}

#[tokio::test]
async fn session_round_events_seq_missing_yields_null_and_status_passthrough() {
    let coord = Arc::new(agentos_session::SessionCoordinator::new());
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let events = SessionRoundEvents::new(
        coord.clone(),
        "thread-sr2",
        "pipe-sr2",
        "u1",
        test_store(),
        "default",
    );
    coord.register_thread("thread-sr2", "u1");

    // 缺 seq（契约外形态）→ null，不伪造序号；blob 带 status → 原样透传
    agentos_engine::RoundEvents::on_round_end(
        &events,
        agentos_engine::RoundEnd {
            round_index: 1,
            message_id: "a_1".into(),
            pipeline_id: "pipe-sr2".into(),
            thread_id: "thread-sr2".into(),
            assistant: Some(json!({
                "role": "assistant", "content": "半截", "status": "interrupted"
            })),
            user_message: None,
        },
    )
    .await;

    let all = frame_types(&frames).await;
    let new_msg = all
        .iter()
        .find(|(t, _)| t == "new_message")
        .map(|(_, v)| v.clone())
        .expect("应有 new_message");
    assert!(
        new_msg["data"]["sequence"].is_null(),
        "缺 seq 必须挂 null（对齐冷恢复零补位契约）: {new_msg}"
    );
    assert_eq!(
        new_msg["data"]["message"]["status"], "interrupted",
        "中断半截消息 status 从 blob 读取"
    );
    let end = all
        .iter()
        .find(|(t, _)| t == "stream_end")
        .map(|(_, v)| v.clone())
        .expect("应有 stream_end");
    assert!(end["data"]["final_sequence"].is_null());
}

#[tokio::test]
async fn session_round_events_message_metadata_passthrough() {
    // 消息 metadata 透传契约（notify_judge 打标消费面，ADR
    // 2026-10-14-ws-new-message-metadata）：blob 携带对象形态 metadata →
    // message.metadata 原样透传；blob 不带或非对象 → 省略该键（只增不删，
    // 与 REST 冷路径 list_session_messages 同形态，冷热同构）。
    let coord = Arc::new(agentos_session::SessionCoordinator::new());
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let events = SessionRoundEvents::new(
        coord.clone(),
        "thread-srm",
        "pipe-srm",
        "u1",
        test_store(),
        "default",
    );
    coord.register_thread("thread-srm", "u1");

    // 有 metadata：notify_worthy 打标透传（前端 toast 弹出唯一依据）
    agentos_engine::RoundEvents::on_round_end(
        &events,
        agentos_engine::RoundEnd {
            round_index: 1,
            message_id: "a_1".into(),
            pipeline_id: "pipe-srm".into(),
            thread_id: "thread-srm".into(),
            assistant: Some(json!({
                "role": "assistant",
                "content": "任务完成",
                "seq": 1,
                "metadata": {"notify_worthy": true}
            })),
            user_message: None,
        },
    )
    .await;
    {
        let all = frame_types(&frames).await;
        let new_msg = all
            .iter()
            .find(|(t, _)| t == "new_message")
            .map(|(_, v)| v.clone())
            .expect("应有 new_message");
        assert_eq!(
            new_msg["data"]["message"]["metadata"]["notify_worthy"], true,
            "blob 对象 metadata 原样透传: {new_msg}"
        );
    }

    // 无 metadata：省略该键（不是 null/空对象，缺省不污染）
    frames.lock().unwrap().clear();
    agentos_engine::RoundEvents::on_round_end(
        &events,
        agentos_engine::RoundEnd {
            round_index: 2,
            message_id: "a_2".into(),
            pipeline_id: "pipe-srm".into(),
            thread_id: "thread-srm".into(),
            assistant: Some(json!({"role": "assistant", "content": "普通回复", "seq": 2})),
            user_message: None,
        },
    )
    .await;
    {
        let all = frame_types(&frames).await;
        let new_msg = all
            .iter()
            .find(|(t, _)| t == "new_message")
            .map(|(_, v)| v.clone())
            .expect("应有 new_message");
        assert!(
            new_msg["data"]["message"].get("metadata").is_none(),
            "无 metadata 轮次省略该键: {new_msg}"
        );
    }

    // 非对象 metadata（契约外形态）：同样省略（对齐冷路径 is_object 守卫）
    frames.lock().unwrap().clear();
    agentos_engine::RoundEvents::on_round_end(
        &events,
        agentos_engine::RoundEnd {
            round_index: 3,
            message_id: "a_3".into(),
            pipeline_id: "pipe-srm".into(),
            thread_id: "thread-srm".into(),
            assistant: Some(json!({
                "role": "assistant", "content": "异形", "seq": 3, "metadata": "junk"
            })),
            user_message: None,
        },
    )
    .await;
    {
        let all = frame_types(&frames).await;
        let new_msg = all
            .iter()
            .find(|(t, _)| t == "new_message")
            .map(|(_, v)| v.clone())
            .expect("应有 new_message");
        assert!(
            new_msg["data"]["message"].get("metadata").is_none(),
            "非对象 metadata 省略该键: {new_msg}"
        );
    }
}

#[tokio::test]
async fn session_round_events_agent_id_passthrough() {
    // 执行身份戳记透传契约：blob 带 agent_id → message.agentId 原样透传
    //（前端气泡卡名/卡头像数据源）；blob 不带 → agentId 为 null（缺省不污染）
    let coord = Arc::new(agentos_session::SessionCoordinator::new());
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let events = SessionRoundEvents::new(
        coord.clone(),
        "thread-sr3",
        "pipe-sr3",
        "u1",
        test_store(),
        "default",
    );
    coord.register_thread("thread-sr3", "u1");

    agentos_engine::RoundEvents::on_round_end(
        &events,
        agentos_engine::RoundEnd {
            round_index: 1,
            message_id: "a_1".into(),
            pipeline_id: "pipe-sr3".into(),
            thread_id: "thread-sr3".into(),
            assistant: Some(json!({
                "role": "assistant", "content": "扮演回复",
                "agent_id": "roleplay_agent", "seq": 3
            })),
            user_message: None,
        },
    )
    .await;
    {
        let all = frame_types(&frames).await;
        let new_msg = all
            .iter()
            .find(|(t, _)| t == "new_message")
            .map(|(_, v)| v.clone())
            .expect("应有 new_message");
        assert_eq!(
            new_msg["data"]["message"]["agentId"], "roleplay_agent",
            "blob 的 agent_id 必须原样透传为 agentId: {new_msg}"
        );
    }

    // 无戳记 blob：agentId 为 null（json! 直引 blob 字段的缺省形态）
    frames.lock().unwrap().clear();
    agentos_engine::RoundEvents::on_round_end(
        &events,
        agentos_engine::RoundEnd {
            round_index: 2,
            message_id: "a_2".into(),
            pipeline_id: "pipe-sr3".into(),
            thread_id: "thread-sr3".into(),
            assistant: Some(json!({"role": "assistant", "content": "普通回复"})),
            user_message: None,
        },
    )
    .await;
    let all = frame_types(&frames).await;
    let new_msg = all
        .iter()
        .find(|(t, _)| t == "new_message")
        .map(|(_, v)| v.clone())
        .expect("应有 new_message");
    assert!(
        new_msg["data"]["message"]["agentId"].is_null(),
        "blob 无 agent_id 时 agentId 必须 null: {new_msg}"
    );
}

// ── 补测：管道解析 / agent 解析失败分支 ───────────────────────────────

#[tokio::test]
async fn resolve_pipeline_id_empty_active_pipeline_yields_empty() {
    // active_pipeline_id 为空串时不得充当执行坐标 → 回退空串（调用方显式处置）
    let mock = ResolveMock::new(Ok(vec![]), Some(session_record(Some(""))));
    assert_eq!(resolve(mock, "T1", "").await, "");
}

#[tokio::test]
async fn dispatch_agent_db_error_falls_back_to_default() {
    // get_session 读取失败：回退默认 agentos（warn 留痕，不 panic 不阻塞派发）
    let registry = agentos_session::ConnectionRegistry::new();
    let mock = ResolveMock::new_failing_get_session(Ok(vec![]));
    assert_eq!(
        resolve_agent(Some(&registry), Some(mock), "").await,
        "agentos",
        "DB 故障回退默认主 agent"
    );
}

// ── 补测：dispatch_interaction_response（声明驱动路由 + 挂起 run 唤醒）──

fn interaction_manifest() -> PluginManifest {
    use agentos_core::traits::{ProtocolRole, ProvidedCapability, ProvidesCapabilities};
    let mut m = manifest_base("interaction_plugin");
    m.provides = Some(ProvidesCapabilities {
        capabilities: vec![ProvidedCapability {
            namespace: "interaction-test-ns".to_string(),
            methods: vec!["respond".to_string()],
            host: Default::default(),
            tool_prefix: None,
            protocol_roles: vec![ProtocolRole {
                role: "interaction-respond".to_string(),
                method: "respond".to_string(),
            }],
        }],
    });
    m
}

/// 记录 (method, params) 的 capability handler，可注入失败。
struct RecordingHandler {
    calls: Mutex<Vec<(String, serde_json::Value)>>,
    fail: bool,
}

#[async_trait]
impl agentos_mcp::CapabilityHandler for RecordingHandler {
    fn namespace(&self) -> &str {
        "interaction-test-ns"
    }
    async fn handle(
        &self,
        method: &str,
        params: serde_json::Value,
    ) -> Result<serde_json::Value, agentos_mcp::McpError> {
        self.calls
            .lock()
            .unwrap()
            .push((method.to_string(), params.clone()));
        if self.fail {
            return Err(agentos_mcp::McpError::Protocol {
                message: "injected handler failure".to_string(),
            });
        }
        Ok(json!({"ok": true}))
    }
}

fn handler_recorder(
    fail: bool,
) -> (
    Arc<RecordingHandler>,
    agentos_mcp::CapabilityHandlerRegistry,
) {
    let handler = Arc::new(RecordingHandler {
        calls: Mutex::new(Vec::new()),
        fail,
    });
    let registry = agentos_mcp::CapabilityHandlerRegistry::new();
    registry.register(handler.clone());
    (handler, registry)
}

#[tokio::test]
async fn interaction_response_without_declared_role_is_ok_noop() {
    // 无插件声明 interaction-respond 角色：fail-closed 跳过应答转发，非错误
    let state = crate::routes::AppState::new();
    let dispatcher = EngineDispatcher::new(state);
    dispatcher
        .dispatch_interaction_response("T1", "req-1", &json!({"response_type": "choice"}))
        .await
        .expect("无声明角色属可跳过场景，不得报错");
}

#[tokio::test]
async fn interaction_response_without_capability_registry_fails() {
    // 声明了角色但 capability registry 未注入：应答转发失败 → Err
    let mut state = crate::routes::AppState::new();
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![interaction_manifest()]));
    let dispatcher = EngineDispatcher::new(state);
    let err = dispatcher
        .dispatch_interaction_response("T1", "req-1", &json!({"response_type": "choice"}))
        .await
        .unwrap_err();
    assert!(
        err.contains("interaction.respond 失败"),
        "registry 缺失必须显式失败: {err}"
    );
}

#[tokio::test]
async fn interaction_response_routes_to_handler_and_resumes_suspended_run() {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    // 挂起 run + resume 凭据（approval/human_interaction suspend 落库形态：
    // state 运行键 + suspend_request_id 凭据）
    store
        .record_run_start(
            "p-s1",
            "default",
            "run-s1",
            "cfg",
            "2026-10-15T00:00:00+00:00",
        )
        .unwrap();
    store
        .set_run_status_projection("p-s1", "default", RunStatus::Suspended)
        .unwrap();
    store
        .upsert_state_field("p-s1", "default", "suspend_request_id", &json!("req-77"))
        .unwrap();

    let (handler, registry) = handler_recorder(false);
    let mut state = crate::routes::AppState::new();
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![interaction_manifest()]));
    state.capability_handlers = Some(Arc::new(registry));
    state.db = Some(store.clone());
    let dispatcher = EngineDispatcher::new(state);

    dispatcher
        .dispatch_interaction_response("T1", "req-77", &json!({"response_type": "approve"}))
        .await
        .expect("声明角色 + registry + 挂起 run 应成功");

    // (A) 路径：应答按声明路由到 handler（载荷信封原样透传）
    {
        let calls = handler.calls.lock().unwrap();
        assert_eq!(calls.len(), 1, "恰好一次 capability 调用");
        assert_eq!(calls[0].0, "respond");
        assert_eq!(calls[0].1["request_id"], "req-77");
        assert_eq!(calls[0].1["response"]["response_type"], "approve");
    }

    // (B) 路径：挂起 run 被 resume（state 运行键簿记回 Running）
    let run = StorageBackend::get_run(store.as_ref(), "run-s1")
        .await
        .unwrap();
    assert_eq!(run.status, RunStatus::Running, "suspended run 必须被唤醒");
}

#[tokio::test]
async fn interaction_response_handler_failure_returns_err() {
    // handler 内部失败 → 应答转发失败传播给调用方
    let (_handler, registry) = handler_recorder(true);
    let mut state = crate::routes::AppState::new();
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![interaction_manifest()]));
    state.capability_handlers = Some(Arc::new(registry));
    let dispatcher = EngineDispatcher::new(state);
    let err = dispatcher
        .dispatch_interaction_response("T1", "req-1", &json!({}))
        .await
        .unwrap_err();
    assert!(err.contains("interaction.respond 失败"), "err: {err}");
}

#[tokio::test]
async fn interaction_response_without_suspended_run_still_ok() {
    // LLM 直调路径无挂起 run：仅 interaction.respond，不算错误
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let (handler, registry) = handler_recorder(false);
    let mut state = crate::routes::AppState::new();
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![interaction_manifest()]));
    state.capability_handlers = Some(Arc::new(registry));
    state.db = Some(store);
    let dispatcher = EngineDispatcher::new(state);

    dispatcher
        .dispatch_interaction_response("T1", "req-miss", &json!({}))
        .await
        .expect("无挂起 run 属正常场景");
    assert_eq!(handler.calls.lock().unwrap().len(), 1, "respond 照常路由");
}

// ── 补测：dispatch_stop 边界（无 store / 无管道 / 队列退回 / 列表故障）──

#[tokio::test]
async fn dispatch_stop_without_store_is_ok() {
    // 无 store（单测/降级模式）：无可落库的传输信号，幂等成功
    let dispatcher = EngineDispatcher::new(crate::routes::AppState::new());
    dispatcher
        .dispatch_stop("T1", "")
        .await
        .expect("无 store 时 stop 幂等成功");
}

#[tokio::test]
async fn dispatch_stop_without_locatable_pipeline_is_idempotent() {
    // 前端未带 pipeline_id 且会话无主管道（无 session 记录）：幂等空转
    let store =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap()) as Arc<dyn StorageBackend>;
    stop_running(store, "T-ghost").await.unwrap();
}

#[tokio::test]
async fn dispatch_stop_returns_pending_queue_with_event() {
    // 用户裁定（2026-09-11）：停止 = 停当前 run + 待处理队列退回输入框
    // （pending_inputs_changed action=returned 发还前端回填）。
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    store
        .create_session(&session_record(Some("ps")))
        .await
        .unwrap();
    StorageBackend::link_pipeline_session(store.as_ref(), "ps", "T1", "default")
        .await
        .unwrap();
    store
        .record_run_start(
            "ps",
            "default",
            "run-q1",
            "cfg",
            "2026-10-15T00:00:00+00:00",
        )
        .unwrap();
    let mk_input = |id: &str, content: &str| agentos_core::types::PendingInputRecord {
        id: id.to_string(),
        pipeline_id: "ps".to_string(),
        tenant_id: "default".to_string(),
        user_id: "u1".to_string(),
        content: content.to_string(),
        thread: "T1".to_string(),
        source: agentos_core::types::PendingInputSource::User,
        agent_id: String::new(),
        route_id: "ps".to_string(),
        thinking_strength: String::new(),
        client_message_id: String::new(),
        execution_context: None,
        state_overlay: None,
        pipeline_config_id: None,
        created_at: chrono::Utc::now().to_rfc3339(),
    };
    store
        .enqueue_pending_input("default", "ps", &mk_input("p_q1", "排队一"))
        .unwrap();
    store
        .enqueue_pending_input("default", "ps", &mk_input("p_q2", "排队二"))
        .unwrap();

    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    let coord = agentos_session::SessionCoordinator::new();
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    coord.register_thread("T1", "u1");
    let mut state = crate::routes::AppState::new();
    state.store = Some(store.clone() as Arc<dyn StorageBackend>);
    seed_default_user(&*store, "u1").await;
    state.session = Some(Arc::new(coord));
    let dispatcher = EngineDispatcher::new(state);

    dispatcher.dispatch_stop("T1", "").await.unwrap();

    // 当前 run 停止（传输信号）
    let run = StorageBackend::get_run(store.as_ref(), "run-q1")
        .await
        .unwrap();
    assert_eq!(run.status, RunStatus::Suspended);
    // 队列退回：表清空 + 事件带全部条目发还
    let rows = store.list_pending_inputs("default", "ps").unwrap();
    assert!(rows.is_empty(), "排队消息必须退回而非被消费，实际 {rows:?}");
    let all = frame_types(&frames).await;
    let evt = all
        .iter()
        .find(|(t, _)| t == "pending_inputs_changed")
        .map(|(_, v)| v.clone())
        .expect("应发 pending_inputs_changed 退回事件");
    assert_eq!(evt["data"]["action"], "returned");
    let items = evt["data"]["items"].as_array().unwrap();
    assert_eq!(items.len(), 2, "两条排队消息全部退回: {evt}");
    let contents: Vec<&str> = items.iter().filter_map(|i| i["content"].as_str()).collect();
    assert!(contents.contains(&"排队一") && contents.contains(&"排队二"));
}

#[tokio::test]
async fn dispatch_stop_tolerates_pending_list_failure() {
    // 队列读取失败：跳过退回（warn），stop 本身仍成功
    let mock = ResolveMock::new(Ok(vec![]), None);
    *mock.list_pending_result.lock().unwrap() = Err(StorageError::Database("injected".to_string()));
    let store = Arc::new(mock) as Arc<dyn StorageBackend>;
    // 前端带 pipeline_id → 停止目标直采，list_runs 走缺省空 → 无 running run；
    // 随后 list_pending_inputs 注入故障。
    stop_pipeline(store, "T1", "p-stop-err").await.unwrap();
}

// ── 补测：pending 消费出队失败收口 / 事件列表故障降级 ─────────────────

#[tokio::test]
async fn drain_pending_queue_notifies_waiters_on_pop_failure() {
    // 消费出队失败：本轮残留条目逐条发 failed outcome（REST chat 同步桥不挂死），
    // 消费任务退出（队列残留待下轮）。
    let mock = ResolveMock::new(Ok(vec![]), None);
    *mock.pop_pending_err.lock().unwrap() = true;
    *mock.list_pending_result.lock().unwrap() = Ok(vec![agentos_core::types::PendingInputRecord {
        id: "p_drain_1".to_string(),
        pipeline_id: "p-drain".to_string(),
        tenant_id: "default".to_string(),
        user_id: "u1".to_string(),
        content: "排队消息".to_string(),
        thread: "T1".to_string(),
        source: agentos_core::types::PendingInputSource::User,
        agent_id: String::new(),
        route_id: "p-drain".to_string(),
        thinking_strength: String::new(),
        client_message_id: "cm-drain-fail-1".to_string(),
        execution_context: None,
        state_overlay: None,
        pipeline_config_id: None,
        created_at: chrono::Utc::now().to_rfc3339(),
    }]);
    let store = Arc::new(mock) as Arc<dyn StorageBackend>;

    let (tx, mut rx) = tokio::sync::oneshot::channel::<crate::server::EngineOutcome>();
    register_outcome_waiter("cm-drain-fail-1".to_string(), tx);

    EngineDispatcher::drain_pending_queue(
        crate::routes::AppState::new(),
        agentos_core::types::TenantContext::new("default".to_string(), "T1".to_string()),
        "T1".to_string(),
        "p-drain".to_string(),
        "u1".to_string(),
        store,
    )
    .await;

    let outcome = rx.try_recv().expect("残留条目必须通知失败 outcome 防挂死");
    assert!(outcome.failed, "出队失败 → failed outcome: {outcome:?}");
    assert!(
        outcome.content.contains("pending 消费出队失败"),
        "失败原因可追溯: {}",
        outcome.content
    );
}

#[tokio::test]
async fn emit_pending_inputs_changed_carries_empty_items_on_list_failure() {
    // 队列列表读取失败：事件照发但 items 为空（warn 留痕，前端同步为空态）
    let mock = ResolveMock::new(Ok(vec![]), None);
    *mock.list_pending_result.lock().unwrap() = Err(StorageError::Database("injected".to_string()));
    let store = Arc::new(mock) as Arc<dyn StorageBackend>;

    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    let coord = agentos_session::SessionCoordinator::new();
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    coord.register_thread("T1", "u1");
    let mut state = crate::routes::AppState::new();
    state.session = Some(Arc::new(coord));

    super::emit_pending_inputs_changed(&state, "T1", "p-x", "default", &store, "enqueued").await;

    let all = frame_types(&frames).await;
    let evt = all
        .iter()
        .find(|(t, _)| t == "pending_inputs_changed")
        .map(|(_, v)| v.clone())
        .expect("列表故障时事件仍应发出（items 空态）");
    assert_eq!(evt["data"]["action"], "enqueued");
    assert!(
        evt["data"]["items"].as_array().unwrap().is_empty(),
        "列表故障不得伪造队列条: {evt}"
    );
}

// ── 补测：handle_inbound 入站帧处理（解析/归属/心跳/路由错误）─────────

struct NoopDispatcher;

#[async_trait]
impl PipelineDispatcher for NoopDispatcher {
    async fn dispatch_user_input(
        &self,
        _thread_id: &str,
        _user_id: &str,
        _content: &str,
        _pipeline_id: &str,
        _thinking_strength: &str,
        _execution_context: Option<&serde_json::Value>,
        _state_overlay: Option<&serde_json::Value>,
        _agent_id: &str,
        _pipeline_config_id: Option<&str>,
        _cmid: &str,
        _source: agentos_core::types::PendingInputSource,
    ) -> Result<(), String> {
        Ok(())
    }
    async fn dispatch_interaction_response(
        &self,
        _thread_id: &str,
        _request_id: &str,
        _response: &serde_json::Value,
    ) -> Result<(), String> {
        Ok(())
    }
    async fn dispatch_stop(&self, _thread_id: &str, _pipeline_id: &str) -> Result<(), String> {
        Ok(())
    }
}

async fn inbound_frames_after(
    text: &str,
    store: Option<&Arc<dyn StorageBackend>>,
    last_sequence: Option<u64>,
) -> Vec<(String, serde_json::Value)> {
    let coord = agentos_session::SessionCoordinator::new();
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let router = InboundRouter::new(Arc::new(NoopDispatcher));
    handle_inbound(
        text,
        "u1",
        &coord,
        &router,
        last_sequence,
        &std::sync::atomic::AtomicBool::new(false),
        store,
    )
    .await;
    frame_types(&frames).await
}

#[tokio::test]
async fn inbound_heartbeat_replies_ack_without_thread_registration() {
    // 心跳必须回 ack（前端连续收不到 ack 判死断连）；无有效 thread_id 不注册
    let frames = inbound_frames_after(
        json!({"type": "heartbeat"}).to_string().as_str(),
        None,
        None,
    )
    .await;
    let ack = frames
        .iter()
        .find(|(t, _)| t == "heartbeat_ack")
        .map(|(_, v)| v.clone())
        .expect("心跳必须回 heartbeat_ack");
    assert!(
        ack["data"]["timestamp"].as_str().is_some(),
        "ack 携带时间戳: {ack}"
    );
}

#[tokio::test]
async fn inbound_empty_thread_id_skips_registration_but_still_ack() {
    // thread_id 空串不构成注册坐标（注册表不收空键），心跳语义照常
    let coord = agentos_session::SessionCoordinator::new();
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let router = InboundRouter::new(Arc::new(NoopDispatcher));
    handle_inbound(
        json!({"type": "heartbeat", "thread_id": ""})
            .to_string()
            .as_str(),
        "u1",
        &coord,
        &router,
        None,
        &std::sync::atomic::AtomicBool::new(false),
        None,
    )
    .await;
    assert!(
        coord.registry().get_user_for_thread("").is_none(),
        "空 thread_id 不得进注册表"
    );
    let all = frame_types(&frames).await;
    assert!(all.iter().any(|(t, _)| t == "heartbeat_ack"));
}

#[tokio::test]
async fn inbound_malformed_json_is_dropped_silently() {
    // 畸形帧丢弃（限频 warn 可观测）：不 panic、无出站帧、无注册
    let coord = agentos_session::SessionCoordinator::new();
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let router = InboundRouter::new(Arc::new(NoopDispatcher));
    handle_inbound(
        "not-json{{{",
        "u1",
        &coord,
        &router,
        None,
        &std::sync::atomic::AtomicBool::new(false),
        None,
    )
    .await;
    assert!(frames.lock().unwrap().is_empty(), "畸形帧不得产生出站帧");
    assert!(coord.registry().get_user_for_thread("T1").is_none());
}

#[tokio::test]
async fn inbound_registers_owned_thread_and_replays_once_per_connection() {
    // 归属校验放行 → 注册 thread→user；B3 回放每连接仅一次（首个带 thread_id
    // 的入站消息触发，此后不再重放）。
    let owned =
        Arc::new(ResolveMock::new(Ok(vec!["P-1".to_string()]), None)) as Arc<dyn StorageBackend>;

    let coord = agentos_session::SessionCoordinator::new();
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let router = InboundRouter::new(Arc::new(NoopDispatcher));
    let replayed = std::sync::atomic::AtomicBool::new(false);
    let frame = json!({"type": "user_input", "thread_id": "T-owned", "content": "hi"});

    // 首帧：注册 + （last_sequence>0 时）回放缺失误差
    handle_inbound(
        frame.to_string().as_str(),
        "u1",
        &coord,
        &router,
        Some(7),
        &replayed,
        Some(&owned),
    )
    .await;
    assert_eq!(
        coord.registry().get_user_for_thread("T-owned").as_deref(),
        Some("u1"),
        "本租户 thread 注册放行"
    );

    // 二帧：注册幂等，回放不二次触发（无重复帧、无 panic）
    handle_inbound(
        frame.to_string().as_str(),
        "u1",
        &coord,
        &router,
        Some(7),
        &replayed,
        Some(&owned),
    )
    .await;
    assert_eq!(
        coord.registry().get_user_for_thread("T-owned").as_deref(),
        Some("u1")
    );
}

#[tokio::test]
async fn inbound_rejects_unowned_thread_registration() {
    // 无管道关联的 thread：不注册（事件单播/回放路由不被外来 thread 劫持）
    let foreign = Arc::new(ResolveMock::new(Ok(vec![]), None)) as Arc<dyn StorageBackend>;
    let coord = agentos_session::SessionCoordinator::new();
    let router = InboundRouter::new(Arc::new(NoopDispatcher));
    handle_inbound(
        json!({"type": "user_input", "thread_id": "T-foreign", "content": "hi"})
            .to_string()
            .as_str(),
        "u1",
        &coord,
        &router,
        None,
        &std::sync::atomic::AtomicBool::new(false),
        Some(&foreign),
    )
    .await;
    assert!(
        coord.registry().get_user_for_thread("T-foreign").is_none(),
        "无关联 thread 必须拒绝注册"
    );
}

#[tokio::test]
async fn inbound_route_error_replies_user_input_ack_failure_frame() {
    // 路由错误（分发失败）必须对客户端可见：回 user_input_ack(ok:false) 错误
    // 帧（带 client_message_id/thread_id/error 原文），warn 仅留痕不再静默吞。
    // （2026-09-29 装机 R302 实证：旧实现只写 WARN，前端「思考中」8 分钟无声。）
    let coord = agentos_session::SessionCoordinator::new();
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let router = InboundRouter::new(Arc::new(ErrUserInputDispatcher));
    handle_inbound(
        json!({
            "type": "user_input",
            "thread_id": "T1",
            "content": "hi",
            "client_message_id": "cm-err-1",
        })
        .to_string()
        .as_str(),
        "u1",
        &coord,
        &router,
        None,
        &std::sync::atomic::AtomicBool::new(false),
        None,
    )
    .await;
    let all = frame_types(&frames).await;
    let ack = all
        .iter()
        .find(|(t, _)| t == "user_input_ack")
        .map(|(_, v)| v.clone())
        .expect("派发失败必须回 user_input_ack(ok:false) 错误帧");
    assert_eq!(ack["ok"], false, "失败回执 ok 必须 false: {ack}");
    assert_eq!(ack["client_message_id"], "cm-err-1", "回执带幂等键: {ack}");
    assert_eq!(ack["thread_id"], "T1", "回执带线程坐标: {ack}");
    let err_text = ack["error"].as_str().unwrap_or_default();
    assert!(
        err_text.contains("injected dispatch failure"),
        "error 携带失败原文: {ack}"
    );
}

#[tokio::test]
async fn inbound_user_input_success_replies_ack_ok_frame() {
    // 派发受理（含直跑/入队两态）即回 user_input_ack(ok:true)——前端恒挂
    // 超时据此撤销；无回执则「已送达」不可知，与失败路径同为盲区。
    let coord = agentos_session::SessionCoordinator::new();
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let router = InboundRouter::new(Arc::new(NoopDispatcher));
    handle_inbound(
        json!({
            "type": "user_input",
            "thread_id": "T1",
            "content": "hi",
            "client_message_id": "cm-ok-1",
        })
        .to_string()
        .as_str(),
        "u1",
        &coord,
        &router,
        None,
        &std::sync::atomic::AtomicBool::new(false),
        None,
    )
    .await;
    let all = frame_types(&frames).await;
    let ack = all
        .iter()
        .find(|(t, _)| t == "user_input_ack")
        .map(|(_, v)| v.clone())
        .expect("派发受理必须回 user_input_ack(ok:true)");
    assert_eq!(ack["ok"], true, "成功回执 ok 必须 true: {ack}");
    assert_eq!(ack["client_message_id"], "cm-ok-1", "回执带幂等键: {ack}");
    assert_eq!(ack["thread_id"], "T1");
    assert!(ack.get("error").is_none(), "成功回执不携带 error 键: {ack}");
}

#[tokio::test]
async fn inbound_ack_reads_client_message_id_from_data_envelope() {
    // cmid 顶层优先、data 信封兜底（与 pipeline_id 同法，契约只增不删）
    let coord = agentos_session::SessionCoordinator::new();
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let router = InboundRouter::new(Arc::new(NoopDispatcher));
    handle_inbound(
        json!({
            "type": "user_input",
            "thread_id": "T1",
            "data": {"content": "hi", "client_message_id": "cm-env-1"},
        })
        .to_string()
        .as_str(),
        "u1",
        &coord,
        &router,
        None,
        &std::sync::atomic::AtomicBool::new(false),
        None,
    )
    .await;
    let all = frame_types(&frames).await;
    let ack = all
        .iter()
        .find(|(t, _)| t == "user_input_ack")
        .map(|(_, v)| v.clone())
        .expect("data 信封 cmid 同样回执");
    assert_eq!(ack["client_message_id"], "cm-env-1", "信封兜底提取: {ack}");
}

#[tokio::test]
async fn inbound_non_user_input_route_error_replies_route_error_frame() {
    // 非 user_input 帧的路由错误回 route_error 通用错误帧（message_type 标明
    // 来源类型），不冒用 user_input_ack 语义；帧字段尽力提取（缺为空串）。
    let coord = agentos_session::SessionCoordinator::new();
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let router = InboundRouter::new(Arc::new(NoopDispatcher));
    handle_inbound(
        // interaction_response 缺 thread_id → RouteOutcome::Error（不经 dispatcher）
        json!({"type": "interaction_response", "data": {"request_id": "r-1"}})
            .to_string()
            .as_str(),
        "u1",
        &coord,
        &router,
        None,
        &std::sync::atomic::AtomicBool::new(false),
        None,
    )
    .await;
    let all = frame_types(&frames).await;
    let err_frame = all
        .iter()
        .find(|(t, _)| t == "route_error")
        .map(|(_, v)| v.clone())
        .expect("非 user_input 路由错误必须回 route_error 帧");
    assert_eq!(
        err_frame["data"]["message_type"], "interaction_response",
        "标明来源消息类型: {err_frame}"
    );
    assert!(
        err_frame["data"]["error"]
            .as_str()
            .unwrap_or_default()
            .contains("interaction_response 缺少 thread_id"),
        "error 携带失败原文: {err_frame}"
    );
    assert!(
        all.iter().all(|(t, _)| t != "user_input_ack"),
        "非 user_input 错误不得冒用 user_input_ack"
    );
}

// ── 补测：轮边界注入消费事件（docs/working/待输入队列轮边界注入_方案_20261025.md）──

#[tokio::test]
async fn session_round_events_pending_consumed_emits_claim_records() {
    // on_pending_inputs_consumed → pending_inputs_changed("consumed")，payload
    // 附加 consumed_messages（逐条权威 record：指纹 record_id/seq/原文）——前端
    // 据此走认领 upgrade/insert（与正常发送同一条 claim 链，终态必携排序键）。
    let coord = Arc::new(agentos_session::SessionCoordinator::new());
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    coord.register_thread("thread-pc", "u1");
    let events = SessionRoundEvents::new(
        coord.clone(),
        "thread-pc",
        "pipe-pc",
        "u1",
        test_store(),
        "default",
    );

    agentos_engine::RoundEvents::on_pending_inputs_consumed(
        &events,
        agentos_engine::PendingConsumed {
            pipeline_id: "pipe-pc".into(),
            client_message_ids: vec!["cmid-1".into(), "cmid-2".into()],
            records: vec![
                agentos_engine::ConsumedPendingRecord {
                    client_message_id: "cmid-1".into(),
                    message_id: "mc_rec1".into(),
                    sequence: 7,
                    content: "排队消息1".into(),
                },
                agentos_engine::ConsumedPendingRecord {
                    client_message_id: "cmid-2".into(),
                    message_id: "mc_rec2".into(),
                    sequence: 8,
                    content: "排队消息2".into(),
                },
            ],
        },
    )
    .await;

    let all = frame_types(&frames).await;
    let ev = all
        .iter()
        .find(|(t, _)| t == "pending_inputs_changed")
        .map(|(_, v)| v.clone())
        .expect("consumed 事件必发");
    assert_eq!(ev["data"]["action"], "consumed");
    assert_eq!(ev["data"]["pipeline_id"], "pipe-pc");
    let records = ev["data"]["consumed_messages"]
        .as_array()
        .expect("consumed_messages 必随 payload 附加（认领回传载荷）");
    assert_eq!(records.len(), 2);
    assert_eq!(records[0]["client_message_id"], "cmid-1");
    assert_eq!(
        records[0]["id"], "mc_rec1",
        "指纹 record_id 必带（claim 幂等键）"
    );
    assert_eq!(records[0]["sequence"], 7, "权威槽位必带（前端排序键）");
    assert_eq!(
        records[0]["content"], "排队消息1",
        "原文必带（气泡缺失时 insert 补插）"
    );
    assert_eq!(records[1]["client_message_id"], "cmid-2");
    assert_eq!(records[1]["sequence"], 8);
}

// ── 入口占位行事件面（ADR 2026-11-06-round-entry-slot-placeholder）──────

#[tokio::test]
async fn stream_start_event_carries_reserved_sequence() {
    // stream_start 增带可选 sequence（只增兼容）：前端占位气泡从第一毫秒起
    // 有权威排序坐标。值为引擎入口保留的槽位（RoundStart.sequence 直传）。
    let coord = Arc::new(agentos_session::SessionCoordinator::new());
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let events = SessionRoundEvents::new(
        coord.clone(),
        "thread-sseq",
        "pipe-sseq",
        "u1",
        test_store(),
        "default",
    );
    agentos_engine::RoundEvents::on_round_start(
        &events,
        agentos_engine::RoundStart {
            round_index: 1,
            message_id: "a_seq".into(),
            sequence: 7,
            pipeline_id: "pipe-sseq".into(),
            thread_id: "thread-sseq".into(),
        },
    )
    .await;
    let all = frame_types(&frames).await;
    let start = all
        .iter()
        .find(|(t, _)| t == "stream_start")
        .map(|(_, v)| v.clone())
        .expect("应有 stream_start");
    assert_eq!(
        start["data"]["sequence"], 7,
        "入口保留 seq 直传（占位气泡权威排序坐标）: {start}"
    );
    assert_eq!(start["data"]["message_id"], "a_seq");
}

#[tokio::test]
async fn round_without_assistant_deletes_entry_placeholder_row() {
    // 轮无 assistant（真零产出）：事件桥 null 占位判定点删除本轮占位行
    // （生命周期封闭于单轮）；有产出轮次桥不删（fill 归出口投影管辖）。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    // 预置引擎形态的入口占位行（seq 7 / record_id=a_del / run_id=前代）
    store
        .apply_messages_ops_to_table(
            "pipe-del",
            "default",
            &[json!({
                "op": "set", "seq": 7,
                "msg": { "role": "assistant", "content": "", "status": "streaming" },
                "_message_id": "a_del",
                "_run_id": "run_prev",
            })],
        )
        .await
        .expect("seed placeholder");

    let coord = Arc::new(agentos_session::SessionCoordinator::new());
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let events = SessionRoundEvents::new(
        coord.clone(),
        "thread-del",
        "pipe-del",
        "u1",
        store.clone(),
        "default",
    );

    agentos_engine::RoundEvents::on_round_start(
        &events,
        agentos_engine::RoundStart {
            round_index: 1,
            message_id: "a_del".into(),
            sequence: 7,
            pipeline_id: "pipe-del".into(),
            thread_id: "thread-del".into(),
        },
    )
    .await;
    agentos_engine::RoundEvents::on_round_end(
        &events,
        agentos_engine::RoundEnd {
            round_index: 1,
            message_id: "a_del".into(),
            pipeline_id: "pipe-del".into(),
            thread_id: "thread-del".into(),
            assistant: None,
            user_message: None,
        },
    )
    .await;

    let rows = store
        .get_messages_by_pipeline("pipe-del", MessageQueryOpts::default())
        .await
        .expect("read rows");
    assert!(
        rows.iter().all(|r| r.seq_in_branch != 7),
        "零产出轮后占位行已被事件桥删除，残留 {:?}",
        rows.iter().map(|r| r.seq_in_branch).collect::<Vec<_>>()
    );

    // 对照：有产出轮次桥不删行（出口投影 fill 同槽管辖，删行是零产出专属）。
    store
        .apply_messages_ops_to_table(
            "pipe-del",
            "default",
            &[json!({
                "op": "set", "seq": 8,
                "msg": { "role": "assistant", "content": "", "status": "streaming" },
                "_message_id": "a_fill",
                "_run_id": "run_prev",
            })],
        )
        .await
        .expect("seed second placeholder");
    agentos_engine::RoundEvents::on_round_start(
        &events,
        agentos_engine::RoundStart {
            round_index: 2,
            message_id: "a_fill".into(),
            sequence: 8,
            pipeline_id: "pipe-del".into(),
            thread_id: "thread-del".into(),
        },
    )
    .await;
    agentos_engine::RoundEvents::on_round_end(
        &events,
        agentos_engine::RoundEnd {
            round_index: 2,
            message_id: "a_fill".into(),
            pipeline_id: "pipe-del".into(),
            thread_id: "thread-del".into(),
            assistant: Some(json!({"role": "assistant", "content": "答", "seq": 8})),
            user_message: None,
        },
    )
    .await;
    let rows = store
        .get_messages_by_pipeline("pipe-del", MessageQueryOpts::default())
        .await
        .expect("read rows");
    assert!(
        rows.iter().any(|r| r.seq_in_branch == 8),
        "有产出轮次桥不删占位槽位（fill 管辖）: {:?}",
        rows.iter().map(|r| r.seq_in_branch).collect::<Vec<_>>()
    );
}
