// @feature: FP-0.2.内核API面 | @vision: V3 可嵌入 | @ci: rust-test
use super::*;

#[test]
fn test_derive_run_terminal_events_completed() {
    let st = json!({"pipeline_id": "p1", "session_id": "th1"});
    let evs = derive_run_terminal_events(&st, false);
    let names: Vec<&str> = evs.iter().map(|(n, _)| *n).collect();
    assert_eq!(names, vec!["run.completed"]);
    // 标签携带运行坐标
    let (_, tags) = &evs[0];
    let tag = |k: &str| {
        tags.iter()
            .find(|(tk, _)| tk.as_str() == k)
            .map(|(_, v)| v.clone())
            .unwrap_or(serde_json::Value::Null)
    };
    assert_eq!(tag("pipeline_id"), json!("p1"));
    assert_eq!(tag("thread_id"), json!("th1"));
    // 任务域字段在 state 也不派生任务事件（派生归 task_service 插件）
    let with_task = json!({
        "pipeline_id": "p1", "task.id": "t9", "task.status": "completed",
        "lineage.parent_pipeline_id": "parent_p1",
    });
    let names2: Vec<&str> = derive_run_terminal_events(&with_task, false)
        .iter()
        .map(|(n, _)| *n)
        .collect();
    assert_eq!(names2, vec!["run.completed"], "内核不得派生 task_completed");
}

// ── 终态 state 载荷（ADR 2026-09-11：内核随事件交出 state，订阅方零回查）──

#[test]
fn test_terminal_payload_carries_plugin_domain_keys_unfiltered() {
    // 断链实况（2026-09-11）：lineage.parent_pipeline_id 不在任何 manifest
    // export_fields 内 → pipeline-state.list 摘要恒缺该键 → 父锚点空串 →
    // 通知与挂号清除双双静默短路。载荷路径不经出口白名单，插件域键必须原样带出。
    let st = json!({
        "pipeline_id": "child1",
        "session_id": "th1",
        "task.id": "child1",
        "task.status": "completed",
        "task.goal": "写周报",
        "lineage.parent_pipeline_id": "parent1",
        "lineage.origin_session_id": "th1",
        "lineage.parent_ws_meta": {"mode": "plain", "path": "/ws"},
    });
    let evs = derive_run_terminal_events(&st, false);
    let (_, tags) = &evs[0];
    let payload = tags
        .iter()
        .find(|(k, _)| k == "state")
        .map(|(_, v)| v.clone())
        .expect("终态事件应携带 state 载荷");
    assert_eq!(payload["lineage.parent_pipeline_id"], json!("parent1"));
    assert_eq!(payload["lineage.origin_session_id"], json!("th1"));
    assert_eq!(payload["lineage.parent_ws_meta"]["path"], json!("/ws"));
    assert_eq!(payload["task.id"], json!("child1"));
    // 内核零知识：不派生任务域事件（载荷只是转发，不是判定）
    let names: Vec<&str> = evs.iter().map(|(n, _)| *n).collect();
    assert_eq!(names, vec!["run.completed"]);
}

#[test]
fn test_terminal_payload_strips_engine_bulk_fields() {
    // 大字段剥离：messages 全历史 / tool_schemas / raw_* / 引擎私有 `_` 前缀键
    // 不进载荷（否则事件到 MB 级，fire-and-forget 通知路径无背压）。
    let st = json!({
        "pipeline_id": "p1",
        "task.status": "completed",
        "messages": [{"role": "user", "content": "x"}],
        "tool_schemas": [{"name": "t"}],
        "raw_result": "big",
        "raw_thinking": "big",
        "raw_tool_calls": [1, 2],
        "_executed_tool_calls": ["a"],
        "track.total_tokens": 42,
    });
    let evs = derive_run_terminal_events(&st, false);
    let payload = evs[0]
        .1
        .iter()
        .find(|(k, _)| k == "state")
        .map(|(_, v)| v.clone())
        .expect("载荷在场");
    for k in [
        "messages",
        "tool_schemas",
        "raw_result",
        "raw_thinking",
        "raw_tool_calls",
        "_executed_tool_calls",
    ] {
        assert!(payload.get(k).is_none(), "{k} 应被剥离");
    }
    // 反向性质：插件域标量键保留
    assert_eq!(payload["track.total_tokens"], json!(42));
    assert_eq!(payload["task.status"], json!("completed"));
}

#[test]
fn test_terminal_payload_absent_or_empty_for_non_object_state() {
    // 非对象 state：载荷为空对象（订阅方回退回查），不 panic
    let evs = derive_run_terminal_events(&json!("not-an-object"), false);
    let payload = evs[0]
        .1
        .iter()
        .find(|(k, _)| k == "state")
        .map(|(_, v)| v.clone())
        .expect("载荷键恒在场（空对象兜底）");
    assert_eq!(payload, json!({}));
}

#[test]
fn test_terminal_payload_present_on_failed_and_suspended_paths() {
    // 四条终态分支同款携带载荷（failed 直通 / 署名映射）
    let st =
        json!({"pipeline_id": "p1", "task.status": "failed", "lineage.parent_pipeline_id": "par"});
    for (name, _) in derive_run_terminal_events(&st, true) {
        assert_eq!(name, "run.failed");
    }
    let failed = derive_run_terminal_events(&st, true);
    let payload = failed[0]
        .1
        .iter()
        .find(|(k, _)| k == "state")
        .map(|(_, v)| v.clone())
        .expect("failed 路径载荷在场");
    assert_eq!(payload["lineage.parent_pipeline_id"], json!("par"));

    let suspended = json!({"pipeline_id": "p2", "suspended": true, "task.id": "t2"});
    let evs = derive_run_terminal_events(&suspended, false);
    assert_eq!(evs[0].0, "run.suspended");
    let payload2 = evs[0]
        .1
        .iter()
        .find(|(k, _)| k == "state")
        .map(|(_, v)| v.clone())
        .expect("suspended 路径载荷在场");
    assert_eq!(payload2["task.id"], json!("t2"));
}

#[test]
fn test_derive_run_terminal_events_plain_and_suspended() {
    let plain = json!({"pipeline_id": "p2", "thread_id": "th2"});
    let names: Vec<&str> = derive_run_terminal_events(&plain, false)
        .iter()
        .map(|(n, _)| *n)
        .collect();
    assert_eq!(names, vec!["run.completed"]);

    // 挂起（RouteNext::Wait 落的 suspended 标志）：run.suspended
    let suspended = json!({"pipeline_id": "p3", "suspended": true});
    let names2: Vec<&str> = derive_run_terminal_events(&suspended, false)
        .iter()
        .map(|(n, _)| *n)
        .collect();
    assert_eq!(names2, vec!["run.suspended"]);
}

#[test]
fn test_derive_run_terminal_events_failed() {
    let st = json!({"pipeline_id": "p4"});
    let names: Vec<&str> = derive_run_terminal_events(&st, true)
        .iter()
        .map(|(n, _)| *n)
        .collect();
    assert_eq!(names, vec!["run.failed"]);
}

#[test]
fn test_derive_run_terminal_events_user_cancelled() {
    // 用户主动停止（router.stop_reason=user_requested）→ run.cancelled
    let st = json!({
        "pipeline_id": "p8",
        "router.stop_reason": "user_requested",
    });
    let names: Vec<&str> = derive_run_terminal_events(&st, false)
        .iter()
        .map(|(n, _)| *n)
        .collect();
    assert_eq!(names, vec!["run.cancelled"]);
}

#[test]
fn test_derive_run_terminal_events_signature_vocabulary() {
    // 终态映射单点（控制状态键契约 ADR 2026-08-30）：落库与域事件共用
    // RunStatus::from_control_state——署名 Failed 的 run 不得广播 run.completed，
    // 任务取消/删除署名与用户停止同落 run.cancelled。
    let cases: Vec<(&str, &str)> = vec![
        ("budget_exhausted", "run.failed"),
        ("elapsed_cap", "run.failed"),
        ("task_failed", "run.failed"),
        ("duplicate_loop", "run.failed"),
        ("tool_fail_loop", "run.failed"),
        ("task_cancelled", "run.cancelled"),
        ("task_deleted", "run.cancelled"),
        ("task_completed", "run.completed"),
        ("", "run.completed"),
    ];
    for (reason, expected) in cases {
        let st = json!({
            "pipeline_id": "p9",
            "router.stop_reason": reason,
        });
        let names: Vec<&str> = derive_run_terminal_events(&st, false)
            .iter()
            .map(|(n, _)| *n)
            .collect();
        assert_eq!(
            names,
            vec![expected],
            "stop_reason={reason} 应派生 {expected}"
        );
    }
}

// ── run.failed 前端镜像事件（run_failed，ADR 2026-09-28） ──────────────

/// 帧捕获 EventSink（ws_session 同款模式）：捕获推到 thread 的 WS 帧。
struct RunFailedCaptureSink {
    frames: Arc<std::sync::Mutex<Vec<String>>>,
}
#[async_trait::async_trait]
impl agentos_session::EventSink for RunFailedCaptureSink {
    async fn send_text(&self, text: &str) -> bool {
        self.frames.lock().unwrap().push(text.to_string());
        true
    }
    fn id(&self) -> u64 {
        46
    }
}

/// 装配仅含 session 的 AppState（CaptureSink 已注册目标 thread）。
fn run_failed_mirror_state(
    frames: Arc<std::sync::Mutex<Vec<String>>>,
    thread_id: &str,
) -> AppState {
    let coord = Arc::new(agentos_session::SessionCoordinator::new());
    coord.register("u-rf", Arc::new(RunFailedCaptureSink { frames }));
    coord.register_thread(thread_id, "u-rf");
    let mut state = AppState::new();
    state.session = Some(coord);
    state
}

/// 引擎 Err 防御路径（failed=true，无署名）→ 恰好一帧 run_failed，
/// 载荷含 pipeline/thread 路由坐标 + status/stop_reason(null)/run_id。
#[tokio::test]
async fn run_failed_mirrored_on_engine_error() {
    let frames = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
    let state = run_failed_mirror_state(frames.clone(), "thread-rf-eng");
    let st = json!({
        "pipeline_id": "pipe-rf-eng",
        "session_id": "thread-rf-eng",
    });
    emit_run_terminal_domain_events(&state, &st, "run-eng-1", true).await;
    let captured = frames.lock().unwrap();
    assert_eq!(captured.len(), 1, "失败终态必须镜像一帧 run_failed");
    let parsed: serde_json::Value = serde_json::from_str(&captured[0]).unwrap();
    assert_eq!(parsed["type"], "run_failed");
    assert_eq!(parsed["data"]["pipeline_id"], "pipe-rf-eng");
    assert_eq!(parsed["data"]["_threadId"], "thread-rf-eng");
    assert_eq!(parsed["data"]["status"], "failed");
    assert!(
        parsed["data"]["stop_reason"].is_null(),
        "无署名失败 stop_reason 为 null"
    );
    assert_eq!(parsed["data"]["run_id"], "run-eng-1");
}

/// stop_reason 署名失败（failed=false，DSL 正常收束——「被插件判定终止」路径）
/// → 同样镜像 run_failed，stop_reason 原值透传（与引擎 Err 路径构成第二组
/// 有区分度输入：failed 参数与署名有无双轴互异）。
#[tokio::test]
async fn run_failed_mirrored_on_signed_stop_reason() {
    for reason in ["tool_fail_loop", "duplicate_loop"] {
        let frames = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
        let state = run_failed_mirror_state(frames.clone(), "thread-rf-sig");
        let st = json!({
            "pipeline_id": "pipe-rf-sig",
            "session_id": "thread-rf-sig",
            "router.stop_reason": reason,
        });
        emit_run_terminal_domain_events(&state, &st, "run-sig-1", false).await;
        let captured = frames.lock().unwrap();
        assert_eq!(captured.len(), 1, "{reason} 应镜像 run_failed");
        let parsed: serde_json::Value = serde_json::from_str(&captured[0]).unwrap();
        assert_eq!(parsed["type"], "run_failed");
        assert_eq!(parsed["data"]["stop_reason"], reason, "署名原值透传");
    }
}

/// 错误透传（ADR 2026-10-27-pipeline-error-notification-channel）：终态带
/// `_plugin_errors` 的失败 run，`error` 字段携带首条插件错误原文（全量不截断）
/// ——折叠失败轮 stop_reason 恒空串，通知卡归因真值随本字段出口。
#[tokio::test]
async fn run_failed_carries_first_plugin_error_verbatim() {
    let frames = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
    let state = run_failed_mirror_state(frames.clone(), "thread-rf-err");
    let st = json!({
        "pipeline_id": "pipe-rf-err",
        "session_id": "thread-rf-err",
        "_plugin_errors": [
            {"plugin_id": "pipeline_llm_core", "code": "MCP_CALL_FAILED", "message": ""},
            {"plugin_id": "pipeline_llm_core", "code": "MCP_CALL_FAILED",
             "message": "RateLimitError - [1302][您的账户已达到速率限制，请您控制请求频率]"},
        ],
    });
    emit_run_terminal_domain_events(&state, &st, "run-err-1", true).await;
    let captured = frames.lock().unwrap();
    assert_eq!(captured.len(), 1);
    let parsed: serde_json::Value = serde_json::from_str(&captured[0]).unwrap();
    // 空文本条目跳过，取首条非空原文（全量透传不截断）
    assert_eq!(
        parsed["data"]["error"],
        "RateLimitError - [1302][您的账户已达到速率限制，请您控制请求频率]"
    );
}

/// 无插件错误的失败轮：error 为 null（键恒在场，前端按非空渲染）。
#[tokio::test]
async fn run_failed_error_null_without_plugin_errors() {
    let frames = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
    let state = run_failed_mirror_state(frames.clone(), "thread-rf-noerr");
    let st = json!({
        "pipeline_id": "pipe-rf-noerr",
        "session_id": "thread-rf-noerr",
    });
    emit_run_terminal_domain_events(&state, &st, "run-noerr-1", true).await;
    let captured = frames.lock().unwrap();
    assert_eq!(captured.len(), 1);
    let parsed: serde_json::Value = serde_json::from_str(&captured[0]).unwrap();
    assert!(
        parsed["data"]["error"].is_null(),
        "无插件错误时 error 为 null"
    );
}

/// 成功/挂起/取消终态不镜像（completed 每轮一帧是噪音；suspended/cancelled
/// 有既有通道）——性质断言：非 failed 终态零帧。
#[tokio::test]
async fn run_failed_not_mirrored_on_non_failed_terminals() {
    for st in [
        json!({"pipeline_id": "p", "session_id": "thread-rf-ok"}),
        json!({"pipeline_id": "p", "session_id": "thread-rf-ok", "suspended": true}),
        json!({"pipeline_id": "p", "session_id": "thread-rf-ok", "router.stop_reason": "user_requested"}),
        json!({"pipeline_id": "p", "session_id": "thread-rf-ok", "router.stop_reason": "task_cancelled"}),
    ] {
        let frames = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
        let state = run_failed_mirror_state(frames.clone(), "thread-rf-ok");
        emit_run_terminal_domain_events(&state, &st, "run-ok-1", false).await;
        assert_eq!(
            frames.lock().unwrap().len(),
            0,
            "非 failed 终态不得镜像 run_failed: {st}"
        );
    }
}

/// 会话坐标缺失（session_id 空——无 WS 投递目标）→ 丢弃不 panic（域事件
/// 照发，镜像仅是前端通道）。
#[tokio::test]
async fn run_failed_dropped_without_thread_coord() {
    let frames = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
    let state = run_failed_mirror_state(frames.clone(), "thread-rf-elsewhere");
    let st = json!({"pipeline_id": "pipe-rf-nothread"});
    emit_run_terminal_domain_events(&state, &st, "run-nothread-1", true).await;
    assert_eq!(frames.lock().unwrap().len(), 0, "无 thread 坐标不得投帧");
}

// ── run 收尾落表字段（run_status + run_stop_reason）──────────────────────

/// 失败归因可透传（事故 2026-10-17 回归）：墙钟超时必须以 `timeout` 署名落库。
/// 真因此前只活在 WS 事件与日志，插件侧对账面读不到，只能写死猜测文案。
#[test]
fn terminal_fields_carry_signed_stop_reason() {
    use agentos_core::types::RunStatus;
    let cases: Vec<(&str, RunStatus)> = vec![
        ("timeout", RunStatus::Failed),
        ("tool_fail_loop", RunStatus::Failed),
        ("max_iterations", RunStatus::Failed),
    ];
    for (reason, expected_status) in cases {
        let st = json!({"pipeline_id": "p-tf", "router.stop_reason": reason});
        let fields = terminal_run_fields(expected_status.clone(), &st);
        assert_eq!(
            fields.get("run_stop_reason").and_then(|v| v.as_str()),
            Some(reason),
            "署名 {reason} 必须原样落库"
        );
        assert_eq!(
            fields.get("run_status"),
            Some(&json!(expected_status)),
            "run_status 必须与署名同批落表"
        );
    }
}

/// 未署名一律落空串（不是跳过）：每轮终态都写，杜绝上一轮署名残留被下一轮
/// 误读为本次归因；键缺席与非字符串形态同归此列。
#[test]
fn terminal_fields_blank_stop_reason_when_unsigned() {
    use agentos_core::types::RunStatus;
    let cases: Vec<serde_json::Value> = vec![
        json!({"pipeline_id": "p-tf"}),
        json!({"pipeline_id": "p-tf", "router.stop_reason": ""}),
        json!({"pipeline_id": "p-tf", "router.stop_reason": 42}),
        json!({"pipeline_id": "p-tf", "router.stop_reason": null}),
    ];
    for st in cases {
        let fields = terminal_run_fields(RunStatus::Failed, &st);
        assert_eq!(
            fields.get("run_stop_reason").and_then(|v| v.as_str()),
            Some(""),
            "未署名必须落空串而非留旧值: {st}"
        );
        assert!(fields.contains_key("run_status"), "run_status 同批落表");
    }
}
