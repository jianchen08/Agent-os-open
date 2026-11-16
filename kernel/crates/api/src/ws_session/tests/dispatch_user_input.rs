use super::*;

use agentos_session::router::InboundRouter;

use super::dispatch_guards::frame_types;

use super::super::no_assistant_reply_content;
use super::support::{
    mint_access_token, next_text_frame, spawn_ws_server, ws_connect, ws_e2e_state,
    ErrUserInputDispatcher,
};
use futures_util::SinkExt as _;
use tokio_tungstenite::tungstenite::Message as WsMessage;

// ── dispatch_user_input：无 store 直跑链 / 忙链持久化入队 ──────────────

/// 失败注入版 e2e state：镜像 `.zctmp/ackproof/proof_silent_drop.py` 实证
/// 脚本场景（派发失败经真 socket 必须可见），区别于默认 SilentDispatcher。
fn ws_e2e_state_err_dispatch(
    session: Arc<agentos_session::SessionCoordinator>,
) -> crate::routes::AppState {
    let mut state = crate::routes::AppState::new();
    state.session = Some(session);
    state.inbound_router = Some(Arc::new(InboundRouter::new(Arc::new(
        ErrUserInputDispatcher,
    ))));
    state
}

#[tokio::test]
async fn ws_user_input_ack_roundtrip_over_real_socket() {
    // 成功路径：真 socket 上 user_input → user_input_ack(ok:true) 先于一切
    // 流式帧返回（会话收发循环全链路，非仅 handle_inbound 单元面）。
    let coord = Arc::new(agentos_session::SessionCoordinator::new());
    let addr = spawn_ws_server(ws_e2e_state(coord.clone())).await;
    let token = mint_access_token("u-ack-1", "AckUser");
    let mut ws = ws_connect(format!("ws://{addr}/ws/chat?token={token}")).await;
    let conf = next_text_frame(&mut ws, "confirmation").await;
    assert_eq!(conf["type"], "connection_confirmation");

    ws.send(WsMessage::text(
        json!({
            "type": "user_input",
            "thread_id": "T-ack-e2e",
            "content": "hi",
            "client_message_id": "cm-e2e-ok",
        })
        .to_string(),
    ))
    .await
    .unwrap();
    let ack = next_text_frame(&mut ws, "user_input_ack").await;
    assert_eq!(ack["type"], "user_input_ack");
    assert_eq!(ack["ok"], true, "受理必须回执 ok=true: {ack}");
    assert_eq!(ack["client_message_id"], "cm-e2e-ok");
    assert_eq!(ack["thread_id"], "T-ack-e2e");
    let _ = ws.send(WsMessage::Close(None)).await;
}

#[tokio::test]
async fn ws_user_input_dispatch_failure_delivers_ack_error_over_real_socket() {
    // 失败路径（python 实证脚本同形）：派发失败在真 socket 上 5s 内必须收到
    // user_input_ack(ok:false)——旧实现此处零回帧（超时 panic 即红）。
    let coord = Arc::new(agentos_session::SessionCoordinator::new());
    let addr = spawn_ws_server(ws_e2e_state_err_dispatch(coord.clone())).await;
    let token = mint_access_token("u-ack-2", "AckUser2");
    let mut ws = ws_connect(format!("ws://{addr}/ws/chat?token={token}")).await;
    let conf = next_text_frame(&mut ws, "confirmation").await;
    assert_eq!(conf["type"], "connection_confirmation");

    ws.send(WsMessage::text(
        json!({
            "type": "user_input",
            "thread_id": "T-ack-fail",
            "content": "hi",
            "client_message_id": "cm-e2e-fail",
        })
        .to_string(),
    ))
    .await
    .unwrap();
    let ack = next_text_frame(&mut ws, "user_input_ack(ok:false)").await;
    assert_eq!(ack["type"], "user_input_ack");
    assert_eq!(ack["ok"], false, "失败必须回执 ok=false: {ack}");
    assert_eq!(ack["client_message_id"], "cm-e2e-fail");
    assert_eq!(ack["thread_id"], "T-ack-fail");
    assert!(
        ack["error"]
            .as_str()
            .unwrap_or_default()
            .contains("injected"),
        "error 带原文: {ack}"
    );
    let _ = ws.send(WsMessage::Close(None)).await;
}

#[tokio::test]
async fn dispatch_user_input_rejects_when_pipeline_missing_without_store() {
    // 无 store：无解析依据，前端空 pipeline_id 即协议违约，显式报错
    let dispatcher = EngineDispatcher::new(crate::routes::AppState::new());
    let err = dispatcher
        .dispatch_user_input(
            "T1",
            "u1",
            "hello",
            "", // pipeline_id 空
            "",
            None,
            None,
            "",
            None,
            "cm-noop",
            agentos_core::types::PendingInputSource::User,
        )
        .await
        .unwrap_err();
    assert!(err.contains("会话无可派发管道"), "实际: {err}");
}

#[tokio::test]
async fn dispatch_user_input_direct_chain_notifies_waiter_with_failure_envelope() {
    // 无 store：消息不经队列直跑（旧行为）。引擎前置依赖缺席 → echo 降级 →
    // 无 assistant 产出 → NO_ASSISTANT_REPLY stream_error 收尾 + failed outcome
    // 通知 REST 同步等待者。
    let coord = Arc::new(agentos_session::SessionCoordinator::new());
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register("u-dc", Arc::new(CapturingSink::with_frames(frames.clone())));
    coord.register_thread("T-dc", "u-dc");
    let mut state = crate::routes::AppState::new();
    state.session = Some(coord);
    let dispatcher = EngineDispatcher::new(state);

    let cmid = format!("cm-dc-{}", uuid::Uuid::new_v4().simple());
    let (tx, rx) = tokio::sync::oneshot::channel::<crate::server::EngineOutcome>();
    register_outcome_waiter(cmid.clone(), tx);
    dispatcher
        .dispatch_user_input(
            "T-dc",
            "u-dc",
            "hello",
            "pipe-dc",
            "",
            None,
            None,
            "",
            None,
            &cmid,
            agentos_core::types::PendingInputSource::User,
        )
        .await
        .unwrap();

    let outcome = tokio::time::timeout(std::time::Duration::from_secs(10), rx)
        .await
        .expect("直跑链必须通知等待者")
        .expect("sender 不应在通知前被 drop");
    assert!(outcome.failed, "无 assistant 产出的直跑轮次必须 failed");
    assert!(outcome.final_assistant.is_none());

    // 事件面（notify 前已发射完毕）：stream_error + run 级收尾
    let all = frame_types(&frames).await;
    let stream_error = all
        .iter()
        .find(|(t, _)| t == "stream_error")
        .map(|(_, v)| v.clone())
        .expect("失败轮次必须发 stream_error");
    assert_eq!(stream_error["data"]["error"]["code"], "NO_ASSISTANT_REPLY");
    assert_eq!(stream_error["data"]["error"]["source"], "kernel");
    let finished = all
        .iter()
        .find(|(t, _)| t == "pipeline_round_finished")
        .map(|(_, v)| v.clone())
        .expect("直跑收尾必须发 pipeline_round_finished");
    assert_eq!(finished["data"]["failed"], true);
    // 文案归因（2026-10-08）：降级链无 plugin_errors → 中性指引，不得
    // 硬编码「检查配置与密钥」误导归因；无插件错误时也不发 plugin_error。
    assert!(
        !stream_error["data"]["error"]["message"]
            .as_str()
            .unwrap()
            .contains("配置与密钥"),
        "无插件错误时兜底文案不得做配置/密钥归因"
    );
    assert!(
        !all.iter().any(|(t, _)| t == "plugin_error"),
        "无插件错误时不得发 plugin_error 事件"
    );
}

#[test]
fn no_assistant_reply_content_carries_real_cause() {
    // plugin_errors 在场 → 附首条非空错误原文（网络/认证等真实异常文本），
    // 不做固定归因（旧文案「请检查 LLM 配置与密钥」把网络故障误报成配置问题）。
    let content = no_assistant_reply_content(&[serde_json::json!({
        "plugin_id": "pipeline_llm_core",
        "code": "PLUGIN_EXEC_FAILED",
        "message": "llm.complete_stream 工具执行失败: Connection error.",
    })]);
    assert!(content.contains("本轮对话未产生回复"));
    assert!(
        content.contains("Connection error."),
        "文案必须携带真实错误原文: {content}"
    );
    assert!(
        !content.contains("配置与密钥"),
        "不得固定归因配置/密钥: {content}"
    );
}

#[test]
fn no_assistant_reply_content_skips_empty_and_takes_first_nonempty() {
    // 空串/缺失 message 跳过，取第一条非空错误（次序敏感）。
    let content = no_assistant_reply_content(&[
        serde_json::json!({"plugin_id": "a", "message": "   "}),
        serde_json::json!({"plugin_id": "b"}),
        serde_json::json!({"plugin_id": "c", "message": "timeout after 300s"}),
    ]);
    assert!(
        content.contains("timeout after 300s"),
        "取首条非空: {content}"
    );
    assert!(
        !content.contains("timeout after 300s   "),
        "原文 trim 后拼接"
    );
}

#[test]
fn no_assistant_reply_content_truncates_long_error() {
    // 超长错误截断到 280 字符 + 省略号（litellm 异常可携带大量上下文）。
    let long_err = "x".repeat(1000);
    let content = no_assistant_reply_content(&[serde_json::json!({
        "plugin_id": "pipeline_llm_core",
        "message": long_err,
    })]);
    let chars: Vec<char> = content.chars().collect();
    assert!(chars.len() < 1000, "超长错误必须截断");
    let prefix_len = "本轮对话未产生回复（模型调用失败：".chars().count();
    assert_eq!(
        chars.len(),
        prefix_len + 280 + 2,
        "280 字符 + 省略号 + 闭合括号"
    );
    assert_eq!(chars[chars.len() - 1], '）', "外层括号闭合");
    assert_eq!(chars[chars.len() - 2], '…', "截断以省略号收尾");
}

#[test]
fn no_assistant_reply_content_neutral_without_plugin_errors() {
    // 无插件错误收集（core 轮次无产出）→ 中性指引。
    let content = no_assistant_reply_content(&[]);
    assert!(content.contains("本轮对话未产生回复"));
    assert!(!content.contains("配置与密钥"));
}

#[tokio::test]
async fn dispatch_user_input_busy_chain_persists_queue_then_drains() {
    // 链忙（占链任务阻塞）：消息落持久化队列（不入直跑）→ 放行后消费任务
    // FIFO 排空队列并通知等待者——条件入队语义（ADR-2026-09-11）。
    let pipe = format!(
        "pipe-bq-{}",
        &uuid::Uuid::new_v4().simple().to_string()[..8]
    );
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    sqlite
        .create_session(&session_record(Some(&pipe)))
        .await
        .unwrap();

    let coord = Arc::new(agentos_session::SessionCoordinator::new());
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register("u-bq", Arc::new(CapturingSink::with_frames(frames.clone())));
    coord.register_thread("T1", "u-bq");
    let mut state = crate::routes::AppState::new();
    state.store = Some(store.clone());
    seed_default_user(&*store, "u-bq").await;
    state.session = Some(coord);
    let dispatcher = EngineDispatcher::new(state);

    // 占链：barrier 任务挂住管道链
    let chain = crate::run_chain::RunChainRegistry::global();
    let (release_tx, release_rx) = tokio::sync::oneshot::channel::<()>();
    chain.enqueue(&pipe, "u-bq", async move {
        let _ = release_rx.await;
    });

    let cmid = format!("cm-bq-{}", uuid::Uuid::new_v4().simple());
    let (tx, rx) = tokio::sync::oneshot::channel::<crate::server::EngineOutcome>();
    register_outcome_waiter(cmid.clone(), tx);
    dispatcher
        .dispatch_user_input(
            "T1",
            "u-bq",
            "排队消息",
            "", // 空 → 解析为主管道 pipe（与占链 key 一致）
            "",
            None,
            None,
            "",
            None,
            &cmid,
            agentos_core::types::PendingInputSource::User,
        )
        .await
        .unwrap();

    // 忙链 → 持久化入队（轮询等待写入可见）
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(5);
    loop {
        let rows = store.list_pending_inputs("default", &pipe).await.unwrap();
        if rows.len() == 1 {
            break;
        }
        assert!(
            tokio::time::Instant::now() < deadline,
            "忙链派发必须落持久化队列"
        );
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }
    // 入队事件先行（前端队列条同步）
    let enqueued = frame_types(&frames).await;
    assert!(
        enqueued
            .iter()
            .any(|(t, v)| t == "pending_inputs_changed" && v["data"]["action"] == "enqueued"),
        "入队必须发 pending_inputs_changed(enqueued): {:?}",
        enqueued.iter().map(|(t, _)| t).collect::<Vec<_>>()
    );

    // 放行占链任务 → 链尾 drain 消费队列 → 等待者收到 outcome
    drop(release_tx);
    let outcome = tokio::time::timeout(std::time::Duration::from_secs(10), rx)
        .await
        .expect("排空后必须通知等待者")
        .expect("sender 不应在通知前被 drop");
    assert!(outcome.failed, "降级执行轮次必须 failed");

    // 队列清空 + consumed 事件 + run 级收尾
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(5);
    loop {
        let rows = store.list_pending_inputs("default", &pipe).await.unwrap();
        if rows.is_empty() {
            break;
        }
        assert!(tokio::time::Instant::now() < deadline, "队列必须被排空");
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }
    let all = frame_types(&frames).await;
    assert!(
        all.iter()
            .any(|(t, v)| t == "pending_inputs_changed" && v["data"]["action"] == "consumed"),
        "消费必须发 pending_inputs_changed(consumed)"
    );
    assert!(
        all.iter().any(|(t, _)| t == "pipeline_round_finished"),
        "消费轮次必须收尾"
    );
}

// ── 直跑早回执（聊天消息流权威化设计真值 20261027 不变量 1）──────────────
// 气泡只认引擎确认：直跑 run 起手 user 消息落库后即时发 user_message_confirmed
// 认领载荷（cmid + record_id + seq + 原文），前端走与正常发送/队列消费同一条
// claimUserMessage 认领链，替代乐观气泡的可见性职责。

/// 最小 LLM 桩：回复固定 assistant 消息（增量 op 模型，与 RecordingInvoker 同构）。
struct MiniLlmInvoker;

#[async_trait::async_trait]
impl agentos_core::traits::PluginInvoker for MiniLlmInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        _plugin_id: &str,
        _ctx: &agentos_core::types::PluginContext<'a>,
    ) -> Result<agentos_core::types::PluginResult, agentos_core::types::PluginError> {
        let reply_msg = serde_json::json!({"role": "assistant", "content": "桩回复"});
        let mut updates = std::collections::HashMap::new();
        updates.insert("raw_result".to_string(), serde_json::json!("桩回复"));
        updates.insert(
            "messages".to_string(),
            serde_json::json!({ "_ops": [{ "op": "set", "msg": reply_msg }] }),
        );
        Ok(agentos_core::types::PluginResult {
            state_updates: updates,
            ..Default::default()
        })
    }
    async fn invoke_tool(
        &self,
        _plugin_id: &str,
        _tool_name: &str,
        _inputs: &serde_json::Value,
    ) -> Result<agentos_core::types::ToolExecutionResult, agentos_core::types::PluginError> {
        Ok(agentos_core::types::ToolExecutionResult::success(
            serde_json::Value::Null,
        ))
    }
    async fn send_lifecycle_hook(
        &self,
        _plugin_id: &str,
        _hook: agentos_core::traits::LifecycleHook,
        _context: &agentos_core::traits::HookContext,
    ) -> Result<(), agentos_core::types::PluginError> {
        Ok(())
    }
    async fn list_plugin_tools(
        &self,
        _plugin_id: &str,
    ) -> Result<serde_json::Value, agentos_core::types::PluginError> {
        Ok(serde_json::json!({ "tools": [] }))
    }
}

#[tokio::test]
async fn user_message_confirmed_emitted_on_direct_run_start() {
    // 直跑链（链空闲）：dispatch_user_input → run 起手 append → 早回执事件
    // 先于/伴随流式到达捕获 sink，载荷携带权威 record（seq/message_id/原文）。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();

    // 临时项目根：autonomous 单例管道引用桩 LLM 插件（与 server make_engine_state 同构）
    let tmp_root = std::env::temp_dir().join(format!("ea_test_{}", uuid::Uuid::new_v4().simple()));
    let _user_space_guard = crate::test_env::pin_user_root(&tmp_root);
    let cfg_dir = tmp_root.join("config").join("pipelines");
    std::fs::create_dir_all(&cfg_dir).unwrap();
    std::fs::write(
        cfg_dir.join("autonomous.yaml"),
        "name: test_early_ack\nloop_bodies:\n  - id: main\n    steps:\n      - id: llm\n        steps:\n          - mock_llm_core\n",
    )
    .unwrap();

    let coordinator = Arc::new(agentos_session::SessionCoordinator::new());
    let frames = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
    coordinator.register_thread("T-ea", "u1");
    coordinator.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));

    let mut state = crate::routes::AppState::new();
    state.store = Some(store.clone());
    state.invoker = Some(Arc::new(MiniLlmInvoker));
    state.project_root = Some(tmp_root);
    state.db = Some(sqlite.clone());
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![serde_json::from_value::<
        agentos_core::traits::PluginManifest,
    >(serde_json::json!({
        "id": "mock_llm_core", "name": "mock_llm_core", "version": "1.0.0",
        "plugin_type": "pipeline", "language": "python",
        "host_type": "sidecar", "entry": "x", "capabilities": {},
    }))
    .unwrap()]));
    state.session = Some(coordinator.clone());
    seed_default_user(&*store, "u1").await;
    // 派发坐标注册：pipeEA 挂 T-ea（resolve_pipeline_id_for_thread 校验成员关系）
    store
        .create_session(&session_record(Some("pipeEA")))
        .await
        .unwrap();
    store
        .link_pipeline_session("pipeEA", "T-ea", "default")
        .await
        .unwrap();

    let dispatcher = EngineDispatcher::new(state);
    dispatcher
        .dispatch_user_input(
            "T-ea",
            "u1",
            "早回执验证",
            "pipeEA",
            "",
            None,
            None,
            "",
            None,
            "cmid-ea-1",
            agentos_core::types::PendingInputSource::User,
        )
        .await
        .unwrap();

    // 直跑链异步起跑：轮询等早回执帧（5s 上限）。
    let confirmed = {
        let mut found = None;
        for _ in 0..100 {
            {
                let emitted = frames.lock().unwrap();
                if let Some(f) = emitted
                    .iter()
                    .find(|f| f.contains("\"user_message_confirmed\""))
                {
                    found = Some(f.clone());
                }
            }
            if found.is_some() {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        }
        found.expect("直跑 run 起手必须发 user_message_confirmed 早回执")
    };
    let payload: serde_json::Value = serde_json::from_str(&confirmed).unwrap();
    assert_eq!(payload["type"], "user_message_confirmed");
    assert_eq!(payload["data"]["pipeline_id"], "pipeEA");
    assert_eq!(payload["data"]["client_message_id"], "cmid-ea-1");
    let record = &payload["data"]["record"];
    assert_eq!(record["content"], "早回执验证", "认领载荷携带原文");
    assert!(
        record["sequence"].as_i64().unwrap_or(-1) >= 0,
        "认领载荷携带权威 seq: {record}"
    );
    let msg_id = record["id"].as_str().unwrap_or("");
    assert!(
        msg_id.starts_with("mc_"),
        "认领载荷携带权威 record_id（内容指纹）: {record}"
    );

    coordinator.registry().unregister(
        "u1",
        coordinator
            .registry()
            .sinks_of_user("u1")
            .first()
            .expect("连接应仍注册")
            .id(),
    );
}
