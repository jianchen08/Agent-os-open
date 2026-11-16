//! 刀2 失败可见性（2026-10-15 装机缺陷清偿计划）：stage_finalize 出口契约。
//!
//! 契约：EngineOutcome.failed 必须折算 run 真实终态——署名失败路径
//! （`router.stop_reason` 命中 core `FAILED_STOP_REASONS` 词表，DSL 正常收束）
//! 对 chat 面如实呈现失败语义，不得以 raw_result 文本冒充成功外发；正常
//! 完成轮行为不变（content = raw_result，failed = false）；挂起非失败。

use super::*;

/// 失败注入复现（BUG-45 机制面）：run 以 dropped_tool_calls 署名终态收束、
/// 引擎已折算 run_status=failed，但聊天面 outcome 仍报成功且外发虚报文本——
/// 主 agent「声称追加完成实际零写入」以此姿态 200 出口。
#[tokio::test]
async fn signed_failure_must_not_report_success_with_claimed_text() {
    let reg = agentos_session::global_registry();
    let tenant = "tenant_finvis_fail";
    let pid = "pipe_finvis_fail";
    let final_state = serde_json::json!({
        "pipeline_id": pid,
        "ended": true,
        "router.stop_reason": "dropped_tool_calls",
        "run_status": "failed",
        "raw_result": "已追加完成",
        "messages": [
            {"role": "user", "content": "把结论追加到文件"},
            {"role": "assistant", "content": "已追加完成"},
        ],
    });
    let outcome = stage_finalize(
        &final_state,
        None,
        tenant,
        pid,
        "thread_finvis",
        "agentos",
        "run_fv1",
    )
    .await;
    reg.remove(tenant, pid);

    assert!(
        outcome.failed,
        "署名失败 run 的出口必须 failed=true，实际: {outcome:?}"
    );
    assert!(
        outcome.content.contains("dropped_tool_calls"),
        "失败语义须携带终态署名，实际: {}",
        outcome.content
    );
    assert_ne!(
        outcome.content, "已追加完成",
        "外发文案不得把虚报文本冒充成功回复"
    );
    // 原文不丢：final_assistant 仍携带完整 assistant 消息（消息面照常持久）。
    let assistant = outcome
        .final_assistant
        .expect("失败轮仍须携带末条 assistant");
    assert_eq!(assistant["content"], "已追加完成");
}

/// 词表穷举（性质断言）：core `FAILED_STOP_REASONS` 单点词表内每个署名都必须
/// 折算 failed=true——新增署名只改词表，本出口同步生效，不另持词汇。
#[tokio::test]
async fn every_failed_stop_reason_word_maps_to_failed_outcome() {
    let reg = agentos_session::global_registry();
    let tenant = "tenant_finvis_words";
    for reason in agentos_core::types::RunStatus::FAILED_STOP_REASONS {
        let pid = format!("pipe_finvis_{reason}");
        let final_state = serde_json::json!({
            "pipeline_id": pid,
            "ended": true,
            "router.stop_reason": reason,
            "raw_result": "文本",
        });
        let outcome = stage_finalize(
            &final_state,
            None,
            tenant,
            &pid,
            "thread_finvis",
            "agentos",
            "run_fv2",
        )
        .await;
        assert!(
            outcome.failed,
            "词表署名 {reason} 必须折算 failed=true，实际: {outcome:?}"
        );
        reg.remove(tenant, &pid);
    }
}

/// 回归：正常完成轮（无失败署名）行为不变——failed=false 且 content 仍是
/// raw_result 原文（成功外发通道零变化）。
#[tokio::test]
async fn completed_run_keeps_success_path_unchanged() {
    let reg = agentos_session::global_registry();
    let tenant = "tenant_finvis_ok";
    let pid = "pipe_finvis_ok";
    let final_state = serde_json::json!({
        "pipeline_id": pid,
        "run_status": "completed",
        "raw_result": "正常回复",
        "messages": [{"role": "assistant", "content": "正常回复"}],
    });
    let outcome = stage_finalize(
        &final_state,
        None,
        tenant,
        pid,
        "thread_finvis",
        "agentos",
        "run_fv3",
    )
    .await;
    reg.remove(tenant, pid);

    assert!(!outcome.failed, "正常完成轮不得误报失败: {outcome:?}");
    assert_eq!(outcome.content, "正常回复", "成功轮外发原文不变");
}

/// 挂起非失败：suspended=true（等待人类交互）时即使残留失败署名形态的键面，
/// 出口仍 failed=false——挂起轮由恢复路径续跑，不是失败终态（from_control_state
/// 挂起优先与落库/域事件同源）。
#[tokio::test]
async fn suspended_run_is_not_failed() {
    let reg = agentos_session::global_registry();
    let tenant = "tenant_finvis_susp";
    let pid = "pipe_finvis_susp";
    let final_state = serde_json::json!({
        "pipeline_id": pid,
        "suspended": true,
        "run_status": "suspended",
        "raw_result": "",
    });
    let outcome = stage_finalize(
        &final_state,
        None,
        tenant,
        pid,
        "thread_finvis",
        "agentos",
        "run_fv4",
    )
    .await;
    reg.remove(tenant, pid);

    assert!(!outcome.failed, "挂起轮非失败终态: {outcome:?}");
}

/// REST chat 响应形状：失败语义随 failed 字段外发（字段只增不删，仿 degraded
/// 先例——HTTP 调用方据此区分成败，不再只看 200 状态码）。
#[test]
fn ws_response_serializes_failed_flag() {
    let make = |failed: bool| WsResponse {
        r#type: "message".to_string(),
        content: "本轮 run 失败（stop_reason: timeout）".to_string(),
        session_id: "s1".to_string(),
        timestamp: "2026-10-15T00:00:00Z".to_string(),
        degraded: None,
        failed: Some(failed),
    };
    let ok: serde_json::Value = serde_json::to_value(make(false)).unwrap();
    let bad: serde_json::Value = serde_json::to_value(make(true)).unwrap();
    assert_eq!(ok["failed"], false, "成功轮必须显式外发 failed=false");
    assert_eq!(bad["failed"], true, "失败轮必须显式外发 failed=true");
    assert_eq!(bad["content"], "本轮 run 失败（stop_reason: timeout）");
}

// ── 失败不进消息流（ADR 2026-10-27-pipeline-error-notification-channel）────
//
// 契约（2026-10-27 用户裁定，替代缺陷 ④-A 失败回复直写）：run 以失败终态
// 收束时，消息存储面（message_slots/blobs）零写入——失败是管道域事实，不
// 是对话内容，聊天流不得出现合成错误气泡（刷新/他端查历史也不出现）。失败
// 事实的常驻面 = run.plugin_errors 常驻镜像 + run_status/run_stop_reason；
// 失败文本的出口 = EngineOutcome.content（REST 响应 / WS stream_error）与
// run_failed 事件 error 字段。断网轮失败回复曾落库（缺陷 ④-A 旧契约），
// 该契约随本裁定整体退役。

use std::sync::Arc as TestArc;

/// 失败轮复现形态（断网轮同构）：插件步失败（llm.complete_stream 连接失败）
/// 被引擎 warn+继续吞掉，零产出折叠 failed（step_error_run_signature_aggregation）。
fn failed_zero_output_state(pid: &str) -> serde_json::Value {
    let conn_error = "MCP call failed: tool call failed: llm_core.execute: \
MCP protocol error: [0] llm.complete_stream 工具执行失败: Connection error.";
    serde_json::json!({
        "pipeline_id": pid,
        "ended": true,
        "run_status": "completed",
        "raw_result": "",
        "_plugin_errors": [{
            "plugin_id": "llm_core",
            "code": "PLUGIN_EXEC_FAILED",
            "message": conn_error,
        }],
        "messages": [
            {"role": "user", "content": "你好", "seq": 0},
        ],
    })
}

#[tokio::test]
async fn failed_round_keeps_message_stream_clean() {
    let reg = agentos_session::global_registry();
    let store = TestArc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let backend: TestArc<dyn agentos_core::traits::StorageBackend> = store.clone();
    let tenant = "tenant_finvis_dbp";
    let pid = "pipe_finvis_dbp";
    // 真实时序：user 消息在 stage_recover_history 已落库（轮首），轮失败后
    // 才到收尾——先播种 user 再 finalize，与生产 seq 空间一致。
    let mut seeded = failed_zero_output_state(pid);
    agentos_engine::apply_messages_op_update(
        &mut seeded,
        backend.as_ref(),
        tenant,
        &[serde_json::json!({"op": "set", "msg": {"role": "user", "content": "你好"}})],
    )
    .await
    .unwrap();
    let final_state = seeded;
    let outcome = stage_finalize(
        &final_state,
        Some(backend.as_ref()),
        tenant,
        pid,
        "thread_finvis_dbp",
        "agentos",
        "run_dbp_1",
    )
    .await;

    assert!(outcome.failed, "零产出折叠失败终态: {outcome:?}");
    let failure_text = outcome.content.clone();
    assert!(
        failure_text.contains("本轮 run 失败"),
        "失败语义文本: {failure_text}"
    );

    // 消息存储面零污染（ADR 2026-10-27 新契约）：失败轮不合成错误消息，
    // 存储面只有本轮 user 消息——聊天流（含刷新/他端历史）不见错误气泡。
    let history = backend.load_message_history(pid, tenant).await.unwrap();
    let assistants: Vec<&serde_json::Value> = history
        .iter()
        .filter(|m| m["role"] == "assistant")
        .collect();
    assert!(
        assistants.is_empty(),
        "失败轮不得落任何 assistant 消息（失败不进消息流），实际: {history:?}"
    );
    assert!(
        history.iter().any(|m| m["role"] == "user"),
        "本轮 user 消息在账: {history:?}"
    );

    // REST 读面（message_slots ⨝ blobs → MessageRecord）：同样零 assistant。
    let records = store
        .get_slot_messages_by_pipeline(
            pid,
            tenant,
            agentos_core::traits::MessageQueryOpts {
                before_sequence: None,
                after_sequence: None,
                limit: None,
            },
        )
        .unwrap();
    assert!(
        !records.iter().any(|r| r.role == "assistant"),
        "REST 读面无失败回复记录: {records:?}"
    );

    // 失败事实常驻面（管道域读面）：run.plugin_errors 镜像键入账（全量错误
    // 数组，面板「错误」行数据源），热冷同源。
    let entry = reg.get(tenant, pid).expect("收尾补建条目");
    let e = entry.read();
    let mirrored = &e.state["run.plugin_errors"];
    assert!(
        mirrored.is_array() && !mirrored.as_array().unwrap().is_empty(),
        "插件级失败常驻镜像在账: {:?}",
        e.state
    );
    assert_eq!(
        mirrored[0]["message"], "MCP call failed: tool call failed: llm_core.execute: MCP protocol error: [0] llm.complete_stream 工具执行失败: Connection error.",
        "镜像错误原文透传（全量不截断）"
    );
    // 消息队列同样零合成（旧 synthetic 剔除逻辑退役后队列天然干净）。
    let reg_msgs = e.state["messages"].as_array().expect("messages 队列");
    assert!(
        !reg_msgs.iter().any(|m| m["role"] == "assistant"),
        "registry 常驻副本不合成失败回复（热冷同构）: {reg_msgs:?}"
    );
    reg.remove(tenant, pid);
}

/// 重入收尾幂等（ADR 2026-10-27 新契约）：同一 final_state 二次 finalize，
/// 存储面仍零 assistant——失败轮不写消息流，重入自然零双写。
#[tokio::test]
async fn failure_round_reentry_persists_nothing() {
    let reg = agentos_session::pipeline_state_registry::global_registry();
    let store = TestArc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let backend: TestArc<dyn agentos_core::traits::StorageBackend> = store.clone();
    let tenant = "tenant_finvis_dbq";
    let pid = "pipe_finvis_dbq";
    // 首轮收尾：user 无 seq（引擎/落库侧分配）——模拟未经历引擎的原始形态。
    let final_state = serde_json::json!({
        "pipeline_id": pid,
        "ended": true,
        "run_status": "completed",
        "raw_result": "",
        "_plugin_errors": [{"plugin_id": "llm_core", "message": "boom"}],
        "messages": [{"role": "user", "content": "你好"}],
    });
    let first = stage_finalize(
        &final_state,
        Some(backend.as_ref()),
        tenant,
        pid,
        "thread_finvis_dbq",
        "agentos",
        "run_dbq_a",
    )
    .await;
    // 重入：模拟同状态二次收尾（防御性幂等，不引入幽灵补发）。
    let second = stage_finalize(
        &final_state,
        Some(backend.as_ref()),
        tenant,
        pid,
        "thread_finvis_dbq",
        "agentos",
        "run_dbq_a",
    )
    .await;
    reg.remove(tenant, pid);

    assert!(first.failed && second.failed);
    let history = backend.load_message_history(pid, tenant).await.unwrap();
    let assistant_count = history.iter().filter(|m| m["role"] == "assistant").count();
    assert_eq!(
        assistant_count, 0,
        "失败轮零落消息（重入亦然）: {history:?}"
    );
}

/// 负控：失败收尾对既有消息面零写（本轮已有的 assistant 产出——如超时半截
/// 内容已由插件落库——保持原样，失败收尾不追加也不覆盖）。
#[tokio::test]
async fn failed_round_with_assistant_output_does_not_double_persist() {
    let reg = agentos_session::pipeline_state_registry::global_registry();
    let store = TestArc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let backend: TestArc<dyn agentos_core::traits::StorageBackend> = store.clone();
    let tenant = "tenant_finvis_dbr";
    let pid = "pipe_finvis_dbr";
    // 引擎侧产出已按 op 模型落库（真实时序：轮内 llm 半截内容先落 slots，
    // 收尾在后）——收尾看到的 state.messages 与存储面一致。
    let mut seeded = serde_json::json!({
        "pipeline_id": pid,
        "ended": true,
        "router.stop_reason": "timeout",
        "run_status": "failed",
    });
    let ops = [
        serde_json::json!({"op": "set", "msg": {"role": "user", "content": "写长文"}}),
        serde_json::json!({
            "op": "set",
            "msg": {"role": "assistant", "content": "写到一半的超时半截内容"},
        }),
    ];
    agentos_engine::apply_messages_op_update(&mut seeded, backend.as_ref(), tenant, &ops)
        .await
        .unwrap();
    let final_state = seeded;
    let outcome = stage_finalize(
        &final_state,
        Some(backend.as_ref()),
        tenant,
        pid,
        "thread_finvis_dbr",
        "agentos",
        "run_dbr_1",
    )
    .await;
    reg.remove(tenant, pid);

    assert!(outcome.failed, "署名超时失败终态");
    let history = backend.load_message_history(pid, tenant).await.unwrap();
    let assistants: Vec<&serde_json::Value> = history
        .iter()
        .filter(|m| m["role"] == "assistant")
        .collect();
    assert_eq!(
        assistants.len(),
        1,
        "本轮已有 assistant 产出，失败收尾零追加: {history:?}"
    );
    assert_eq!(
        assistants[0]["content"], "写到一半的超时半截内容",
        "原 assistant 消息不被失败文本覆盖"
    );
}

/// 负控：成功轮零落失败回复（成功轮 assistant 由引擎 op 链落库，收尾不添笔）。
#[tokio::test]
async fn completed_round_persists_no_failure_reply() {
    let reg = agentos_session::pipeline_state_registry::global_registry();
    let store = TestArc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let backend: TestArc<dyn agentos_core::traits::StorageBackend> = store.clone();
    let tenant = "tenant_finvis_dbs";
    let pid = "pipe_finvis_dbs";
    let final_state = serde_json::json!({
        "pipeline_id": pid,
        "run_status": "completed",
        "raw_result": "正常回复",
        "messages": [{"role": "user", "content": "你好", "seq": 0}],
    });
    let outcome = stage_finalize(
        &final_state,
        Some(backend.as_ref()),
        tenant,
        pid,
        "thread_finvis_dbs",
        "agentos",
        "run_dbs_1",
    )
    .await;
    reg.remove(tenant, pid);

    assert!(!outcome.failed);
    let history = backend.load_message_history(pid, tenant).await.unwrap();
    assert!(
        history.iter().all(|m| m["role"] != "assistant"),
        "成功轮收尾零追加 assistant: {history:?}"
    );
}

// ── 插件级失败常驻镜像（ADR 2026-10-17-task-birth-workspace-inclusion）────
//
// 契约：`_plugin_errors` 是引擎私有 staging 键（`_` 前缀），strip_non_resident_keys
// 落库前剥离 → 重启后插件级失败彻底失忆，启动对账（tasks/reconcile）无从知悉。
// 收尾须把非空插件级失败镜像到常驻键 `run.plugin_errors`：内存快照与
// pipeline_state 表双写（冷启动后 pipeline-state.list 仍可读）。
// 不改 ADR 2026-10-15 的折叠语义（有产出 + 步失败 = degraded 仍成立）——
// 本键只负责让失败事实不丢。

#[tokio::test]
async fn plugin_errors_mirrored_to_resident_key() {
    let reg = agentos_session::global_registry();
    let store = TestArc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let backend: TestArc<dyn agentos_core::traits::StorageBackend> = store.clone();
    let tenant = "tenant_finvis_pe";
    let pid = "pipe_finvis_pe";
    // 有产出 + 插件步失败 = degraded 形态（ADR 2026-10-15 选定完成态）
    let final_state = serde_json::json!({
        "pipeline_id": pid,
        "run_status": "completed",
        "raw_result": "半截文本",
        "_plugin_errors": [{
            "plugin_id": "workspace_lifecycle",
            "code": "PLUGIN_EXEC_FAILED",
            "message": "worktree 分化未落地",
        }],
        "messages": [{"role": "user", "content": "你好", "seq": 0}],
    });
    let outcome = stage_finalize(
        &final_state,
        Some(backend.as_ref()),
        tenant,
        pid,
        "thread_finvis_pe",
        "agentos",
        "run_pe_1",
    )
    .await;
    reg.remove(tenant, pid);

    assert!(!outcome.failed, "有产出 + 步失败 = degraded 非 failed");
    assert!(outcome.degraded, "该形态应落 degraded");

    // 冷读面：镜像键在 pipeline_state 表里（重启后 pipeline-state.list 的源）
    let persisted = backend.load_pipeline_state(pid, tenant).await.unwrap();
    let mirrored = persisted.get("run.plugin_errors").expect("镜像键须落表");
    let arr = mirrored.as_array().expect("镜像键为数组");
    assert_eq!(arr.len(), 1, "失败条数须与引擎收集一致: {arr:?}");
    assert_eq!(arr[0]["plugin_id"], "workspace_lifecycle");
    assert_eq!(arr[0]["message"], "worktree 分化未落地");
}

#[tokio::test]
async fn clean_run_does_not_write_resident_key() {
    let reg = agentos_session::global_registry();
    let store = TestArc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let backend: TestArc<dyn agentos_core::traits::StorageBackend> = store.clone();
    let tenant = "tenant_finvis_pen";
    let pid = "pipe_finvis_pen";
    let final_state = serde_json::json!({
        "pipeline_id": pid,
        "run_status": "completed",
        "raw_result": "正常回复",
        "_plugin_errors": [],
        "messages": [{"role": "user", "content": "你好", "seq": 0}],
    });
    stage_finalize(
        &final_state,
        Some(backend.as_ref()),
        tenant,
        pid,
        "thread_finvis_pen",
        "agentos",
        "run_pen_1",
    )
    .await;
    reg.remove(tenant, pid);

    let persisted = backend.load_pipeline_state(pid, tenant).await.unwrap();
    assert!(
        !persisted.contains_key("run.plugin_errors"),
        "零插件失败不得留空数组键（常驻面不增体积）: {persisted:?}"
    );
}
