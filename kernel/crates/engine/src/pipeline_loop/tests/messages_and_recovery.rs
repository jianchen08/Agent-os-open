// @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: rust-test
//! 消息账目与恢复（messages chars / pending 输入 / lifecycle / merge）。

use super::*;
/// 公式门真假两侧 + 冷启动兜底侧：项级 when 对 state 求值决定 guard 是否执行。
#[tokio::test]
async fn context_gate_formula_when_controls_guard_item() {
    let gate = || StepItem::Gated {
        name: "guard".into(),
        when: Some(CONTEXT_GATE_WHEN.into()),
        inputs: HashMap::new(),
    };
    let config = gated_body(vec![gate()]);

    // 低于阈值：(60000 + 1000*2)/128000 = 0.484 ≤ 0.6 → guard 零调用
    // （state 形态对齐生产：track.llm_usage 为平键持 dict——前缀平键解析命中）
    let below = Fixture::build(&["guard"]);
    below
        .run(
            &config,
            &StepLibrary::default(),
            json!({
                "track.llm_usage": {"last_input_tokens": 60000},
                "track.messages_chars": 9000,
                "track.messages_chars_at_llm": 8000,
                "track.model_context_window": 128000,
                "messages": [{"role": "user", "content": "hi"}],
            }),
        )
        .await;
    assert_eq!(
        below.invoker.call_count("guard"),
        0,
        "占用比低于阈值跳过 guard"
    );

    // 超过阈值：(78000 + 51000*2)/128000 = 1.41 > 0.6 → guard 放行
    let above = Fixture::build(&["guard"]);
    above
        .run(
            &config,
            &StepLibrary::default(),
            json!({
                "track.llm_usage": {"last_input_tokens": 78000},
                "track.messages_chars": 59000,
                "track.messages_chars_at_llm": 8000,
                "track.model_context_window": 128000,
                "messages": [{"role": "user", "content": "hi"}],
            }),
        )
        .await;
    assert_eq!(
        above.invoker.call_count("guard"),
        1,
        "占用比超阈值放行 guard"
    );

    // 冷启动兜底：无 token 锚（公式支 fail-soft 判假）且消息 41 条 → 放行精确判定
    let cold = Fixture::build(&["guard"]);
    let msgs: Vec<serde_json::Value> = (0..41)
        .map(|i| json!({"seq": i, "role": "user", "content": format!("m{i}")}))
        .collect();
    cold.run(
        &config,
        &StepLibrary::default(),
        json!({"messages": msgs, "track.messages_chars": 4100}),
    )
    .await;
    assert_eq!(
        cold.invoker.call_count("guard"),
        1,
        "无锚且消息多时兜底放行"
    );

    // 区分度反向：无锚但消息不多（≤40）→ 两个析取支均假 → 跳过
    let cold_small = Fixture::build(&["guard"]);
    cold_small
        .run(
            &config,
            &StepLibrary::default(),
            json!({"messages": [{"role": "user", "content": "hi"}]}),
        )
        .await;
    assert_eq!(
        cold_small.invoker.call_count("guard"),
        0,
        "无锚且消息少不兜底"
    );
}
#[tokio::test]
async fn pending_message_ops_recorded_as_user_input_trace_and_stripped() {
    let fixture = Fixture::build(&[]);
    let config = noop_pipeline_config();
    let state = fixture
        .run(
            &config,
            &StepLibrary::default(),
            json!({
                "pipeline_id": "p_user",
                "session_id": "s_user",
                "messages": [{"role": "user", "content": "首轮", "seq": 0}],
                "_pending_message_ops": [{"op": "set", "seq": 0, "msg": {"role": "user"}}],
            }),
        )
        .await;

    let traces = fixture.store.trace_plugin_ids.lock().unwrap().clone();
    assert!(
        traces.iter().any(|p| p == "user_input"),
        "首轮 user 实录必须落轨迹（审计/回放范围），实际 {traces:?}"
    );
    assert!(
        state.get("_pending_message_ops").is_none(),
        "内部通道字段必须从 state 移除（不泄漏进快照/投影）"
    );
}
#[tokio::test]
async fn empty_pending_message_ops_records_no_user_input_trace() {
    // 空数组是合法输入（无 user 消息轮，如后台续跑）：不得落空轨迹
    let fixture = Fixture::build(&[]);
    let config = noop_pipeline_config();
    fixture
        .run(
            &config,
            &StepLibrary::default(),
            json!({
                "pipeline_id": "p_user_empty",
                "session_id": "s_user",
                "_pending_message_ops": [],
            }),
        )
        .await;
    let traces = fixture.store.trace_plugin_ids.lock().unwrap().clone();
    assert!(
        !traces.iter().any(|p| p == "user_input"),
        "空 ops 不得产生空实录，实际 {traces:?}"
    );

    // 非数组形态（写入方误传）同样静默跳过，不 panic
    let fixture2 = Fixture::build(&[]);
    fixture2
        .run(
            &config,
            &StepLibrary::default(),
            json!({
                "pipeline_id": "p_user_bad",
                "session_id": "s_user",
                "_pending_message_ops": {"not": "an array"},
            }),
        )
        .await;
    let traces2 = fixture2.store.trace_plugin_ids.lock().unwrap().clone();
    assert!(
        !traces2.iter().any(|p| p == "user_input"),
        "非数组形态不得产生实录，实际 {traces2:?}"
    );
}
#[test]
fn abnormal_end_signature_table() {
    // 纯函数表驱动：异常特征各归其词，正常收束/挂起/已署名一律 None。
    let calls = json!([{ "name": "resource_search", "arguments": {} }]);
    let cases: Vec<(serde_json::Value, Option<&str>)> = vec![
        // 未署名 + 未消费工具调用 → dropped_tool_calls
        (
            json!({ "raw_tool_calls": calls }),
            Some("dropped_tool_calls"),
        ),
        // stop_reason 为空串同未署名（stop_check 无触发轮复位后的形态）
        (
            json!({ "router.stop_reason": "", "raw_tool_calls": calls }),
            Some("dropped_tool_calls"),
        ),
        // 未署名 + 空回复耗尽未裁决 → empty_reply_exhausted
        (
            json!({ "llm_empty_exhausted": true }),
            Some("empty_reply_exhausted"),
        ),
        // 挂起（等待恢复）非终局，恢复后调用继续消费
        (json!({ "suspended": true, "raw_tool_calls": calls }), None),
        // 已署名 = 终止方权威，引擎不覆写
        (
            json!({ "router.stop_reason": "task_completed", "raw_tool_calls": calls }),
            None,
        ),
        // 正常收束：调用已消费（空数组/缺失/非数组）、无耗尽信号
        (json!({ "raw_tool_calls": [] }), None),
        (json!({}), None),
        (json!({ "raw_tool_calls": "not-a-list" }), None),
        (json!({ "llm_empty_exhausted": false }), None),
    ];
    for (state, expected) in cases {
        assert_eq!(abnormal_end_signature(&state), expected, "state={state}");
    }
}
#[tokio::test]
async fn abnormal_end_with_pending_tool_calls_fails_loudly() {
    // 复刻 2026-10-08 会话管道断链：post 路由漏配回环（仅兜底 end），llm
    // 产出工具调用 → 收尾异常终局闸署名 dropped_tool_calls 折算 Failed，
    // 调用被吞不再静默假绿 completed。
    let fixture = Fixture::build(&["pipeline_llm_core"]);
    fixture.invoker.set_result(
        "pipeline_llm_core",
        PluginResult {
            state_updates: updates(&[
                (
                    "raw_tool_calls",
                    json!([{ "name": "resource_search", "arguments": {} }]),
                ),
                ("raw_result", json!("好嘞～先把文件工具加载上。")),
            ]),
            ..Default::default()
        },
    );
    let config = PipelineConfig::single_body(
        "gate",
        None,
        vec![atomic_step("llm", "pipeline_llm_core"), bare_end_post()],
    );
    let final_state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(
        final_state["router.stop_reason"],
        json!("dropped_tool_calls"),
        "异常终局必须署名失败词进轨迹/state"
    );
    assert_eq!(final_state["run_status"], json!("failed"));
    assert_eq!(
        fixture.store.recorded_run_statuses(),
        vec![RunStatus::Failed],
        "落库终态同步 Failed（UI 可见报错）"
    );
}
#[tokio::test]
async fn abnormal_end_authored_stop_reason_not_overwritten() {
    // 已署名终局权威在署名方：任务终态收束（task_completed）遗留在场调用
    // 不翻 Failed，闸门零介入。
    let fixture = Fixture::build(&["pipeline_llm_core"]);
    fixture.invoker.set_result(
        "pipeline_llm_core",
        PluginResult {
            state_updates: updates(&[
                (
                    "raw_tool_calls",
                    json!([{ "name": "task_submit", "arguments": {} }]),
                ),
                ("router.stop_reason", json!("task_completed")),
            ]),
            ..Default::default()
        },
    );
    let config = PipelineConfig::single_body(
        "gate",
        None,
        vec![atomic_step("llm", "pipeline_llm_core"), bare_end_post()],
    );
    let final_state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(
        final_state["router.stop_reason"],
        json!("task_completed"),
        "署名方权威，引擎不得覆写"
    );
    assert_eq!(
        fixture.store.recorded_run_statuses(),
        vec![RunStatus::Completed]
    );
}
#[tokio::test]
async fn abnormal_end_empty_reply_exhausted_fails_loudly() {
    // 会话管道空回复耗尽（无任务域收束方）：agent 零产出即退出 = 异常终局，
    // 闸门署名 empty_reply_exhausted 折算 Failed。
    let fixture = Fixture::build(&["pipeline_llm_core"]);
    fixture.invoker.set_result(
        "pipeline_llm_core",
        PluginResult {
            state_updates: updates(&[
                ("llm_empty_exhausted", json!(true)),
                ("raw_tool_calls", json!([])),
            ]),
            ..Default::default()
        },
    );
    let config = PipelineConfig::single_body(
        "gate",
        None,
        vec![atomic_step("llm", "pipeline_llm_core"), bare_end_post()],
    );
    let final_state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(
        final_state["router.stop_reason"],
        json!("empty_reply_exhausted")
    );
    assert_eq!(final_state["run_status"], json!("failed"));
    assert_eq!(
        fixture.store.recorded_run_statuses(),
        vec![RunStatus::Failed]
    );
}
#[tokio::test]
async fn normal_unsigned_close_without_abnormal_signature_stays_completed() {
    // 会话"文本即停"正常收束（无署名、调用已消费、无耗尽信号）：闸门零
    // 介入，Completed 映射不漂移。
    let fixture = Fixture::build(&["pipeline_llm_core"]);
    fixture.invoker.set_result(
        "pipeline_llm_core",
        PluginResult {
            state_updates: updates(&[
                ("raw_tool_calls", json!([])),
                ("raw_result", json!("老板好呀～我是灵汐。")),
            ]),
            ..Default::default()
        },
    );
    let config = PipelineConfig::single_body(
        "gate",
        None,
        vec![atomic_step("llm", "pipeline_llm_core"), bare_end_post()],
    );
    let final_state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(final_state["run_status"], json!("completed"));
    assert_eq!(
        fixture.store.recorded_run_statuses(),
        vec![RunStatus::Completed]
    );
}
#[tokio::test]
async fn suspended_with_pending_calls_not_failed() {
    // 挂起等待恢复非终局：pending 调用在场不署名（恢复后由 tool_exec 消费）。
    let fixture = Fixture::build(&[]);
    let config = PipelineConfig::single_body(
        "gate",
        None,
        vec![PipelineStep {
            id: "wait".into(),
            steps: vec![],
            when: None,
            context: HashMap::new(),
            routes: vec![Route {
                when: "True".into(),
                then: agentos_core::types::RouteAction {
                    next: RouteNext::Wait,
                    set: HashMap::new(),
                },
            }],
            loop_config: None,
        }],
    );
    let initial = json!({ "raw_tool_calls": [{ "name": "file_read", "arguments": {} }] });
    let final_state = fixture.run(&config, &StepLibrary::default(), initial).await;
    assert_eq!(final_state["suspended"], json!(true));
    assert_eq!(
        final_state.get("router.stop_reason"),
        None,
        "挂起路径不得署名失败词"
    );
    assert_eq!(
        fixture.store.recorded_run_statuses(),
        vec![RunStatus::Suspended]
    );
}
#[tokio::test]
async fn test_foreach_parallel_collect_order_and_consume() {
    // 有界并行：每迭代一次调用；collect 键按迭代下标序拼接（确定性）；
    // consume 清 over 源键；consume_keys 清预填结果；迭代变量不泄漏。
    let inv = Arc::new(ForeachInvoker::new(false));
    let executor = make_executor(inv.clone(), &["tc"]);
    let state = executor
        .run_compiled(
            &compile_pipeline(
                &foreach_pipeline(foreach_step(foreach_loop_config(4))),
                &StepLibrary::default(),
                &executor.plugin_ids,
            )
            .expect("compile"),
            foreach_initial(&["a", "b", "c"]),
        )
        .await
        .unwrap();
    let mut seen = inv.seen_calls.lock().unwrap().clone();
    seen.sort_by(|x, y| {
        x["id"]
            .as_str()
            .unwrap_or("")
            .cmp(y["id"].as_str().unwrap_or(""))
    });
    let want: Vec<serde_json::Value> = ["a", "b", "c"]
        .iter()
        .map(|c| json!({"name": "bash", "id": c}))
        .collect();
    assert_eq!(seen, want, "每迭代恰好一次，元素即 current_call");
    // 归并顺序 = 迭代下标序（确定性），与完成顺序无关
    let echo = |id: &str| json!({"echo": {"name": "bash", "id": id}});
    assert_eq!(
        state["tool_results"],
        json!([echo("a"), echo("b"), echo("c")]),
        "collect 按迭代序拼接"
    );
    assert_eq!(state["_full_tool_results"][0]["full"]["id"], json!("a"));
    assert_eq!(
        state["_executed_tool_calls"].as_array().map(Vec::len),
        Some(3)
    );
    // consume：源键清空；consume_keys：预填结果清除
    assert_eq!(state["raw_tool_calls"], json!([]), "consume 清 over 源键");
    assert_eq!(
        state["pre_decided_results"],
        json!([]),
        "consume_keys 清预填"
    );
    assert!(
        state.get("current_call").is_none() || state["current_call"].is_null(),
        "迭代变量不得泄漏进共享 state"
    );
}
#[tokio::test]
async fn test_foreach_parallel_shares_invariant_keys_via_base() {
    // 写集克隆（用户裁定 2026-11-17）：并行迭代只克隆写集/小键，messages 等
    // 不变量大键进共享基座（Arc），插件边界按 state.reads 从基座回填——
    // 插件视角与全量 state 一致，内存成本与迭代数解耦。
    let inv = Arc::new(
        ForeachInvoker::new(false)
            .with_declared_reads(vec!["messages".into(), "current_call".into()]),
    );
    let executor = make_executor(inv.clone(), &["tc"]);
    let mut initial = foreach_initial(&["a", "b", "c"]);
    initial["messages"] = json!([{"role": "user", "content": "历史消息"}]);
    let state = executor
        .run_compiled(
            &compile_pipeline(
                &foreach_pipeline(foreach_step(foreach_loop_config(4))),
                &StepLibrary::default(),
                &executor.plugin_ids,
            )
            .expect("compile"),
            initial,
        )
        .await
        .unwrap();

    let seen = inv.seen_base.lock().unwrap().clone();
    assert_eq!(seen.len(), 3, "每迭代一次调用");
    for (state_has_messages, base_has_messages) in seen {
        assert!(!state_has_messages, "迭代 state 不得携带共享大键 messages");
        assert!(base_has_messages, "插件边界必须经 base_state 回填 messages");
    }
    assert_eq!(
        state["messages"],
        json!([{"role": "user", "content": "历史消息"}]),
        "共享基座只读，父 state 的 messages 不得被迭代改动"
    );
    assert_eq!(
        state["tool_results"].as_array().map(Vec::len),
        Some(3),
        "写集照常按迭代序归并"
    );
}
#[tokio::test]
async fn test_foreach_serial_chain_visibility() {
    // max_concurrency=1 串行链式：第 N 迭代能看到第 N-1 迭代归并后的写面。
    let inv = Arc::new(ForeachInvoker::new(false));
    let executor = make_executor(inv.clone(), &["tc"]);
    executor
        .run_compiled(
            &compile_pipeline(
                &foreach_pipeline(foreach_step(foreach_loop_config(1))),
                &StepLibrary::default(),
                &executor.plugin_ids,
            )
            .expect("compile"),
            foreach_initial(&["a", "b", "c"]),
        )
        .await
        .unwrap();
    assert_eq!(
        *inv.seen_chain.lock().unwrap(),
        vec![
            json!(null),
            json!({"name": "bash", "id": "a"}),
            json!({"name": "bash", "id": "b"})
        ],
        "链式可见：迭代读到上一迭代的 chain 写面"
    );
}
#[tokio::test]
async fn test_foreach_serial_breaks_on_ended() {
    // 串行链式：某迭代置 ended → 后续迭代不再执行（同串行循环 break 语义）。
    let mut inv2 = ForeachInvoker::new(false);
    inv2.end_on = Some(json!({"name": "bash", "id": "b"}));
    let inv = Arc::new(inv2);
    let executor = make_executor(inv.clone(), &["tc"]);
    let state = executor
        .run_compiled(
            &compile_pipeline(
                &foreach_pipeline(foreach_step(foreach_loop_config(1))),
                &StepLibrary::default(),
                &executor.plugin_ids,
            )
            .expect("compile"),
            foreach_initial(&["a", "b", "c"]),
        )
        .await
        .unwrap();
    assert_eq!(inv.seen_calls.lock().unwrap().len(), 2, "ended 后 c 不执行");
    assert_eq!(state["ended"], json!(true), "ended 传播到父 state");
}
#[tokio::test]
async fn test_foreach_parallel_ended_propagates() {
    // 并行快照：全部迭代照跑，任一迭代置 ended → 父置位（OR 归并）。
    let mut inv2 = ForeachInvoker::new(false);
    inv2.end_on = Some(json!({"name": "bash", "id": "b"}));
    let inv = Arc::new(inv2);
    let executor = make_executor(inv.clone(), &["tc"]);
    let state = executor
        .run_compiled(
            &compile_pipeline(
                &foreach_pipeline(foreach_step(foreach_loop_config(4))),
                &StepLibrary::default(),
                &executor.plugin_ids,
            )
            .expect("compile"),
            foreach_initial(&["a", "b", "c"]),
        )
        .await
        .unwrap();
    assert_eq!(
        inv.seen_calls.lock().unwrap().len(),
        3,
        "并行下全部迭代照跑"
    );
    assert_eq!(state["ended"], json!(true));
}
#[tokio::test]
async fn test_foreach_max_iterations_caps_items() {
    // 安全阀：迭代元素数被 max_iterations 截断。
    let inv = Arc::new(ForeachInvoker::new(false));
    let executor = make_executor(inv.clone(), &["tc"]);
    let mut lc = foreach_loop_config(-1);
    lc.max_iterations = 2;
    executor
        .run_compiled(
            &compile_pipeline(
                &foreach_pipeline(foreach_step(lc)),
                &StepLibrary::default(),
                &executor.plugin_ids,
            )
            .expect("compile"),
            foreach_initial(&["a", "b", "c", "d", "e"]),
        )
        .await
        .unwrap();
    assert_eq!(
        inv.seen_calls.lock().unwrap().len(),
        2,
        "max_iterations 截断迭代数"
    );
}
#[tokio::test]
async fn test_foreach_empty_over_still_consumes() {
    // 空批：零调用；consume/consume_keys 清 [] 照常生效（幂等收尾）。
    let inv = Arc::new(ForeachInvoker::new(false));
    let executor = make_executor(inv.clone(), &["tc"]);
    let state = executor
        .run_compiled(
            &compile_pipeline(
                &foreach_pipeline(foreach_step(foreach_loop_config(4))),
                &StepLibrary::default(),
                &executor.plugin_ids,
            )
            .expect("compile"),
            json!({"raw_tool_calls": [], "pre_decided_results": [{"tool_name": "bash"}]}),
        )
        .await
        .unwrap();
    assert_eq!(inv.seen_calls.lock().unwrap().len(), 0);
    assert_eq!(state["raw_tool_calls"], json!([]));
    assert_eq!(state["pre_decided_results"], json!([]));
    assert_eq!(state["tool_results"], json!([]), "collect 键清空占位");
}
#[tokio::test]
async fn test_foreach_single_item_over() {
    // 单元素边界：单调用即执行，collect/collect_append/consume 语义与多元素一致。
    let inv = Arc::new(ForeachInvoker::new(false));
    let executor = make_executor(inv.clone(), &["tc"]);
    let state = executor
        .run_compiled(
            &compile_pipeline(
                &foreach_pipeline(foreach_step(foreach_loop_config(4))),
                &StepLibrary::default(),
                &executor.plugin_ids,
            )
            .expect("compile"),
            foreach_initial(&["only"]),
        )
        .await
        .unwrap();
    assert_eq!(inv.seen_calls.lock().unwrap().len(), 1);
    assert_eq!(
        state["tool_results"],
        json!([{"echo": {"name": "bash", "id": "only"}}]),
        "单元素 collect 拼接即该迭代产出"
    );
    assert_eq!(
        state["submitted_task_ids"],
        json!(["t0", {"name": "bash", "id": "only"}]),
        "单元素累计键 = 父现值 + 一条增量"
    );
    assert_eq!(state["raw_tool_calls"], json!([]), "consume 照常清源");
}
#[tokio::test]
async fn test_foreach_messages_ops_applied_in_iteration_order() {
    // messages ops 父侧按迭代序统一 apply：数组顺序 = 迭代下标序（确定性）。
    let inv = Arc::new(ForeachInvoker::new(true));
    let executor = make_executor(inv.clone(), &["tc"]);
    let state = executor
        .run_compiled(
            &compile_pipeline(
                &foreach_pipeline(foreach_step(foreach_loop_config(4))),
                &StepLibrary::default(),
                &executor.plugin_ids,
            )
            .expect("compile"),
            foreach_initial(&["a", "b", "c"]),
        )
        .await
        .unwrap();
    let msgs = state["messages"].as_array().expect("messages array");
    let ids: Vec<&str> = msgs
        .iter()
        .map(|m| {
            m["tool_call_id"]["id"]
                .as_str()
                .unwrap_or(m["tool_call_id"].as_str().unwrap_or(""))
        })
        .collect();
    assert_eq!(ids, vec!["a", "b", "c"], "messages 归并序 = 迭代下标序");
}
#[tokio::test]
async fn test_foreach_collect_append_cumulative() {
    // collect_append 累计键：不清空，父现值 + 各迭代增量按迭代序拼接。
    let inv = Arc::new(ForeachInvoker::new(false));
    let executor = make_executor(inv.clone(), &["tc"]);
    let state = executor
        .run_compiled(
            &compile_pipeline(
                &foreach_pipeline(foreach_step(foreach_loop_config(4))),
                &StepLibrary::default(),
                &executor.plugin_ids,
            )
            .expect("compile"),
            foreach_initial(&["a", "b", "c"]),
        )
        .await
        .unwrap();
    assert_eq!(
        state["submitted_task_ids"],
        json!([
            "t0",
            {"name": "bash", "id": "a"},
            {"name": "bash", "id": "b"},
            {"name": "bash", "id": "c"}
        ]),
        "累计键 = 父现值 + 增量（快照基不丢失）"
    );
}
#[tokio::test]
async fn test_foreach_collect_cleared_each_round_while_append_grows() {
    // 跨轮行为对照（同一 run 内循环体两轮进入同一 for-each step）：collect 键
    // 每轮清 [] 只留本轮产出；collect_append 键跨轮累计不清。
    let inv = Arc::new(CrossRoundInvoker {
        feed_no: AtomicUsize::new(0),
        stop_no: AtomicUsize::new(0),
        seen_submitted: Mutex::new(Vec::new()),
    });
    let executor = make_executor(inv.clone(), &["feed", "tc", "stopper"]);
    let config = PipelineConfig {
        name: "foreach_cross_round".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![
                PipelineStep {
                    id: "feed_step".into(),
                    steps: vec!["feed".into()],
                    when: None,
                    context: HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                },
                foreach_step(foreach_loop_config(4)),
                PipelineStep {
                    id: "stop_step".into(),
                    steps: vec!["stopper".into()],
                    when: None,
                    context: HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                },
            ],
            while_cond: Some("True".into()),
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let state = executor
        .run_compiled(
            &compile_pipeline(&config, &StepLibrary::default(), &executor.plugin_ids)
                .expect("compile"),
            foreach_initial(&[]),
        )
        .await
        .unwrap();
    // 第一轮 feed 时父侧只有初始 t0；第二轮 feed 时已含第一轮增量 a、b——
    // collect_append 跨轮累计的插件视角直接观察。
    assert_eq!(
        *inv.seen_submitted.lock().unwrap(),
        vec![
            json!(["t0"]),
            json!(["t0", {"name": "bash", "id": "a"}, {"name": "bash", "id": "b"}]),
        ],
        "第二轮迭代入口看到第一轮累计"
    );
    assert_eq!(
        state["tool_results"],
        json!([{"echo": {"name": "bash", "id": "c"}}]),
        "collect 每轮清空：第二轮不残留第一轮产出"
    );
    assert_eq!(
        state["submitted_task_ids"],
        json!([
            "t0",
            {"name": "bash", "id": "a"},
            {"name": "bash", "id": "b"},
            {"name": "bash", "id": "c"}
        ]),
        "collect_append 跨轮累计：第二轮在第一轮终值上追加"
    );
}
#[tokio::test]
async fn test_foreach_plugin_errors_aggregated_not_appended() {
    // 插件错误账本（用户裁定 2026-11-17）：逐迭代失败按 plugin_id 聚合，
    // run 收尾物化为每插件一条结论（count=失败次数）——不再按迭代追加数组。
    let fixture_inv = Arc::new(MockInvoker::new());
    fixture_inv.set_err(
        "tc",
        PluginError {
            message: "sidecar unreachable".into(),
            code: None,
            source: None,
        },
    );
    let executor = make_executor(fixture_inv.clone(), &["tc"]);
    let state = executor
        .run_compiled(
            &compile_pipeline(
                &foreach_pipeline(foreach_step(foreach_loop_config(2))),
                &StepLibrary::default(),
                &executor.plugin_ids,
            )
            .expect("compile"),
            foreach_initial(&["a", "b", "c"]),
        )
        .await
        .unwrap();
    let errs = state["_plugin_errors"].as_array().expect("errors array");
    assert_eq!(errs.len(), 1, "每插件一条聚合结论");
    assert_eq!(errs[0]["plugin_id"], "tc");
    assert_eq!(errs[0]["count"], 3, "迭代失败计数累计");
    assert_eq!(errs[0]["message"], "sidecar unreachable");
    assert!(
        state
            .get("_plugin_error_stats")
            .and_then(|v| v.as_array())
            .is_some(),
        "运行期账本存在（_ 前缀不落库）"
    );
}
#[test]
fn test_foreach_compile_validation() {
    let plugin_ids: std::collections::HashSet<String> = ["tc".to_string()].into();
    let mk = |lc: LoopConfig| foreach_pipeline(foreach_step(lc));
    let compile = |config: &PipelineConfig| {
        compile_pipeline(config, &StepLibrary::default(), &plugin_ids)
            .expect_err("expect compile error")
            .to_string()
    };
    // over 缺 as
    let lc = LoopConfig {
        enabled: true,
        max_iterations: -1,
        over: Some("state.raw_tool_calls".into()),
        ..Default::default()
    };
    assert!(compile(&mk(lc)).contains("成对"), "over/as 必须成对");
    // as 有而 over 缺（反向不成对）
    let lc = LoopConfig {
        enabled: true,
        max_iterations: -1,
        as_iter: Some("c".into()),
        ..Default::default()
    };
    assert!(compile(&mk(lc)).contains("成对"), "as 缺 over 同样拒绝");
    // max_concurrency=0
    let lc = LoopConfig {
        enabled: true,
        max_iterations: -1,
        over: Some("state.x".into()),
        as_iter: Some("c".into()),
        max_concurrency: 0,
        ..Default::default()
    };
    assert!(compile(&mk(lc)).contains("max_concurrency"), "0 非法");
    // collect 出现在重复循环上
    let lc = LoopConfig {
        enabled: true,
        max_iterations: -1,
        collect: vec!["k".into()],
        ..Default::default()
    };
    assert!(compile(&mk(lc)).contains("仅 for-each"), "collect 需 over");
    // over 非 state. 形式
    let lc = LoopConfig {
        enabled: true,
        max_iterations: -1,
        over: Some("{{state.x}}".into()),
        as_iter: Some("c".into()),
        ..Default::default()
    };
    assert!(compile(&mk(lc)).contains("state."), "over 必须字面形式");
    // as 撞保留键
    let lc = LoopConfig {
        enabled: true,
        max_iterations: -1,
        over: Some("state.x".into()),
        as_iter: Some("messages".into()),
        ..Default::default()
    };
    assert!(compile(&mk(lc)).contains("保留键"), "as 禁用保留名");
    // 归并键含点号
    let lc = LoopConfig {
        enabled: true,
        max_iterations: -1,
        over: Some("state.x".into()),
        as_iter: Some("c".into()),
        collect: vec!["a.b".into()],
        ..Default::default()
    };
    assert!(compile(&mk(lc)).contains("归并键"), "归并键禁点号");
    // 合法配置编译通过（回归护栏：校验不误伤）
    assert!(
        compile_pipeline(
            &mk(foreach_loop_config(4)),
            &StepLibrary::default(),
            &plugin_ids
        )
        .is_ok(),
        "合法 for-each 配置必须编译通过"
    );
}
/// run_compiled 入口接线：run 到达即以管道引用集（referenced_plugin_ids）调用
/// ensure_hosts_ready，且先于任何步骤 invoke（"到达即并行拉起"语义锚）；
/// 空引用集管道零调用。
#[tokio::test]
async fn run_compiled_ensures_chain_hosts_ready_before_first_step() {
    let invoker = Arc::new(EnsureRecordingInvoker {
        events: Mutex::new(Vec::new()),
    });
    let store: Arc<dyn StorageBackend> = Arc::new(NullStorage::default());
    let executor = PipelineExecutor::new(
        invoker.clone() as Arc<dyn PluginInvoker>,
        PathBuf::from("."),
        TenantContext::new("t", "s"),
        ["chain_a", "chain_b"].iter().map(|s| s.to_string()),
        store,
        "run_ensure",
    );
    let config = PipelineConfig {
        name: "chain".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![PipelineStep {
                id: "body".into(),
                when: None,
                steps: vec![
                    StepItem::Bare("chain_b".into()),
                    StepItem::Bare("chain_a".into()),
                ],
                context: HashMap::new(),
                routes: vec![],
                loop_config: None,
            }],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let compiled =
        compile_pipeline(&config, &StepLibrary::default(), &executor.plugin_ids).expect("编译成功");
    executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("run 成功");

    let events = invoker.events.lock().unwrap().clone();
    assert_eq!(
        events.first().map(String::as_str),
        Some("ensure:chain_a,chain_b"),
        "run 入口先拉起全链（引用集无序归一），先于任何步骤: {events:?}"
    );
    assert_eq!(
        events.iter().filter(|e| e.starts_with("ensure:")).count(),
        1,
        "ensure 每 run 恰一次（循环轮内不重复拉起）"
    );
    assert!(
        events.iter().any(|e| e == "invoke:chain_a")
            && events.iter().any(|e| e == "invoke:chain_b"),
        "拉起后步骤照常执行: {events:?}"
    );
}
/// 空引用集（无步骤）管道：ensure 零调用（空集零开销）。
#[tokio::test]
async fn run_compiled_skips_ensure_for_empty_reference_set() {
    let invoker = Arc::new(EnsureRecordingInvoker {
        events: Mutex::new(Vec::new()),
    });
    let store: Arc<dyn StorageBackend> = Arc::new(NullStorage::default());
    let executor = PipelineExecutor::new(
        invoker.clone() as Arc<dyn PluginInvoker>,
        PathBuf::from("."),
        TenantContext::new("t", "s"),
        std::iter::empty::<String>(),
        store,
        "run_ensure_empty",
    );
    let config = PipelineConfig {
        name: "empty".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![PipelineStep {
                id: "body".into(),
                when: None,
                steps: vec![],
                context: HashMap::new(),
                routes: vec![],
                loop_config: None,
            }],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let compiled =
        compile_pipeline(&config, &StepLibrary::default(), &executor.plugin_ids).expect("编译成功");
    executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("run 成功");
    assert!(
        invoker
            .events
            .lock()
            .unwrap()
            .iter()
            .all(|e| !e.starts_with("ensure:")),
        "空引用集不得调用 ensure: {:?}",
        invoker.events.lock().unwrap()
    );
}
#[tokio::test]
async fn pending_inputs_injected_before_llm_call_fifo_and_queue_drained() {
    // run 在途且队列有条目 → 首轮 LLM 调用前注入为 user 消息（FIFO 序），
    // 队列清空、cmid 记入 run 内部键、消息落 message_slots。
    let sqlite = Arc::new(SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let counter = Arc::new(AtomicUsize::new(0));
    let invoker = Arc::new(InjectScenarioInvoker {
        counter: counter.clone(),
        results: vec![
            updates(&[("flag", json!(true))]),
            updates(&[("flag", json!(false))]),
        ],
        seen_messages: Mutex::new(Vec::new()),
        store: None,
        enqueue_tenant: String::new(),
        enqueue_on_call: HashMap::new(),
    });
    let executor = PipelineExecutor::new(
        invoker.clone() as Arc<dyn PluginInvoker>,
        PathBuf::from("."),
        TenantContext::new("tenant_inject", "session_inject"),
        std::iter::once("p".to_string()),
        store.clone(),
        "run_inject",
    );
    store
        .enqueue_pending_input(
            "tenant_inject",
            &pending_record("pipe-inject", "排队消息A", "cmid-a", "2026-10-25T00:00:01Z"),
        )
        .await
        .unwrap();
    store
        .enqueue_pending_input(
            "tenant_inject",
            &pending_record("pipe-inject", "排队消息B", "cmid-b", "2026-10-25T00:00:02Z"),
        )
        .await
        .unwrap();

    let config = injection_loop_body(vec![StepItem::Bare("p".into())]);
    let final_state = executor
        .run_compiled(
            &compile_pipeline(&config, &StepLibrary::default(), &executor.plugin_ids)
                .expect("compile should succeed"),
            json!({"pipeline_id": "pipe-inject", "session_id": "thread_inject"}),
        )
        .await
        .unwrap();

    let seen = invoker.seen_messages.lock().unwrap().clone();
    assert!(!seen.is_empty(), "循环体至少执行一轮");
    let first_call_users: Vec<&str> = seen[0]
        .iter()
        .filter(|m| m["role"] == json!("user"))
        .filter_map(|m| m["content"].as_str())
        .collect();
    assert_eq!(
        first_call_users,
        vec!["排队消息A", "排队消息B"],
        "首轮 LLM 调用前队列必须已注入且 FIFO 序"
    );
    let remaining = store
        .list_pending_inputs("tenant_inject", "pipe-inject")
        .await
        .unwrap();
    assert!(remaining.is_empty(), "注入后队列表必须清空");
    assert_eq!(
        final_state["_consumed_pending_cmids"],
        json!(["cmid-a", "cmid-b"]),
        "消费 cmid 必须记入 run 内部键（stage_finalize 提取面）"
    );
    // get_messages_by_pipeline 读路径按 task-local 租户作用域过滤（current_or_default）
    let persisted = agentos_tenant::scope(
        TenantContext::new("tenant_inject", "session_inject"),
        store.get_messages_by_pipeline(
            "pipe-inject",
            agentos_core::traits::MessageQueryOpts {
                before_sequence: None,
                after_sequence: None,
                limit: None,
            },
        ),
    )
    .await
    .unwrap();
    let persisted_users: Vec<Option<&String>> = persisted
        .iter()
        .filter(|m| m.role == "user")
        .map(|m| m.content_preview.as_ref())
        .collect();
    assert!(
        persisted_users
            .iter()
            .any(|p| p.is_some_and(|c| c.contains("排队消息A")))
            && persisted_users
                .iter()
                .any(|p| p.is_some_and(|c| c.contains("排队消息B"))),
        "注入消息必须落 message_slots（持久化可查），实际 {persisted_users:?}"
    );
}
#[tokio::test]
async fn pending_inputs_not_consumed_at_tool_execute_iteration() {
    // iter1（llm_call）执行期间新消息 X 入队（模拟 LLM 调用中到达）→ 路由写
    // core_type=tool_execute → iter2（工具迭代）不消费（call2 快照无 X）→
    // 路由写回 llm_call → iter3 开轮前消费 X（call3 快照有 X）。
    let sqlite = Arc::new(SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let counter = Arc::new(AtomicUsize::new(0));
    let invoker = Arc::new(InjectScenarioInvoker {
        counter: counter.clone(),
        results: vec![
            updates(&[("flag", json!(true)), ("core_type", json!("tool_execute"))]),
            updates(&[("flag", json!(true)), ("core_type", json!("llm_call"))]),
            updates(&[("flag", json!(false))]),
        ],
        seen_messages: Mutex::new(Vec::new()),
        store: Some(store.clone()),
        enqueue_tenant: "tenant_inject".to_string(),
        enqueue_on_call: [(
            0usize,
            pending_record(
                "pipe-inject2",
                "途中消息X",
                "cmid-x",
                "2026-10-25T00:00:03Z",
            ),
        )]
        .into_iter()
        .collect(),
    });
    let executor = PipelineExecutor::new(
        invoker.clone() as Arc<dyn PluginInvoker>,
        PathBuf::from("."),
        TenantContext::new("tenant_inject", "session_inject"),
        std::iter::once("p".to_string()),
        store.clone(),
        "run_inject2",
    );

    let config = injection_loop_body(vec![StepItem::Bare("p".into())]);
    let final_state = executor
        .run_compiled(
            &compile_pipeline(&config, &StepLibrary::default(), &executor.plugin_ids)
                .expect("compile should succeed"),
            json!({"pipeline_id": "pipe-inject2", "session_id": "thread_inject"}),
        )
        .await
        .unwrap();

    let seen = invoker.seen_messages.lock().unwrap().clone();
    assert_eq!(seen.len(), 3, "三次调用：llm → tool → llm");
    let has_x = |msgs: &[serde_json::Value]| {
        msgs.iter()
            .any(|m| m["role"] == json!("user") && m["content"] == json!("途中消息X"))
    };
    assert!(
        !has_x(&seen[0]),
        "call1 时 X 尚未入队（call1 执行中才入队）"
    );
    assert!(
        !has_x(&seen[1]),
        "call2 是 tool_execute 迭代：必须不消费，X 仍留队列"
    );
    assert!(has_x(&seen[2]), "iter3 llm_call 开轮前必须注入 X");
    assert!(
        store
            .list_pending_inputs("tenant_inject", "pipe-inject2")
            .await
            .unwrap()
            .is_empty(),
        "iter3 注入后队列清空"
    );
    assert_eq!(
        final_state["_consumed_pending_cmids"],
        json!(["cmid-x"]),
        "工具迭代不消费的条目由下一 LLM 迭代补消费"
    );
}
#[tokio::test]
async fn round_end_user_claim_prefers_trigger_message_after_injection() {
    // 注入发生过的 run：round_end 的 user 认领回传必须仍指向 run 触发消息
    // （前端乐观气泡按其 cmid 认领，不得被注入消息抢占）；未注入 run 行为
    // 不变（last user）。
    let store: Arc<dyn StorageBackend> = Arc::new(NullStorage::default());
    let capture = Arc::new(RoundEndCapture {
        ends: Mutex::new(Vec::new()),
    });
    let executor = PipelineExecutor::new(
        Arc::new(MockInvoker::new()) as Arc<dyn PluginInvoker>,
        PathBuf::from("."),
        TenantContext::new("t_claim", "s_claim"),
        std::iter::empty::<String>(),
        store,
        "run_claim",
    )
    .with_round_events(capture.clone());
    let trigger = json!({"role": "user", "content": "触发消息", "seq": 0});
    let state = json!({
        "pipeline_id": "pipe-claim",
        "session_id": "thread-claim",
        "messages": [
            trigger,
            {"role": "user", "content": "注入消息", "seq": 1},
            {"role": "assistant", "content": "回复", "seq": 2},
        ],
        "_consumed_pending_cmids": ["cmid-x"],
    });
    let start = RoundStart {
        round_index: 1,
        message_id: "a_claim1".into(),
        sequence: 3,
        pipeline_id: "pipe-claim".into(),
        thread_id: "thread-claim".into(),
    };
    executor
        .round_end_event(
            &state,
            1,
            "a_claim1".into(),
            &start,
            0,
            Some(json!({"role": "user", "content": "触发消息", "seq": 0})),
        )
        .await;
    let ends = capture.ends.lock().unwrap().clone();
    assert_eq!(ends.len(), 1);
    assert_eq!(
        ends[0]
            .user_message
            .as_ref()
            .and_then(|u| u["content"].as_str()),
        Some("触发消息"),
        "注入发生过的 run：认领回传必须指向触发消息而非末条注入消息"
    );

    // 对照：未注入 run（无 _consumed_pending_cmids）→ last user（行为不变）。
    capture.ends.lock().unwrap().clear();
    let state_no_inject = json!({
        "pipeline_id": "pipe-claim",
        "session_id": "thread-claim",
        "messages": [
            {"role": "user", "content": "触发消息", "seq": 0},
            {"role": "user", "content": "后续消息", "seq": 1},
        ],
    });
    executor
        .round_end_event(&state_no_inject, 1, "a_claim1".into(), &start, 0, None)
        .await;
    let ends = capture.ends.lock().unwrap().clone();
    assert_eq!(
        ends[0]
            .user_message
            .as_ref()
            .and_then(|u| u["content"].as_str()),
        Some("后续消息"),
        "未注入 run：last user 语义不变"
    );
}
#[tokio::test]
async fn pending_consumed_carries_authoritative_records_for_claim() {
    // 排序键不变量（终态必携权威序号）：注入消费回调必须逐条携带权威 record
    // （指纹 record_id / 引擎槽位 seq / 原文），前端据此走认领 upgrade/insert
    // ——与正常发送同一条 claim 链，不再退化成无 key 原地升级（无 seq 消息按
    // 排序键永久垫底的根因）。cmid 条目一一配对，FIFO 序即槽位序。
    let sqlite = Arc::new(SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let counter = Arc::new(AtomicUsize::new(0));
    let invoker = Arc::new(InjectScenarioInvoker {
        counter: counter.clone(),
        results: vec![
            updates(&[("flag", json!(true))]),
            updates(&[("flag", json!(false))]),
        ],
        seen_messages: Mutex::new(Vec::new()),
        store: None,
        enqueue_tenant: String::new(),
        enqueue_on_call: HashMap::new(),
    });
    let capture = Arc::new(ConsumedCapture {
        events: Mutex::new(Vec::new()),
    });
    let executor = PipelineExecutor::new(
        invoker.clone() as Arc<dyn PluginInvoker>,
        PathBuf::from("."),
        TenantContext::new("tenant_inject", "session_inject"),
        std::iter::once("p".to_string()),
        store.clone(),
        "run_inject_rec",
    )
    .with_round_events(capture.clone());
    store
        .enqueue_pending_input(
            "tenant_inject",
            &pending_record("pipe-inject", "排队消息A", "cmid-a", "2026-10-25T00:00:01Z"),
        )
        .await
        .unwrap();
    store
        .enqueue_pending_input(
            "tenant_inject",
            &pending_record("pipe-inject", "排队消息B", "cmid-b", "2026-10-25T00:00:02Z"),
        )
        .await
        .unwrap();

    let config = injection_loop_body(vec![StepItem::Bare("p".into())]);
    executor
        .run_compiled(
            &compile_pipeline(&config, &StepLibrary::default(), &executor.plugin_ids)
                .expect("compile should succeed"),
            json!({"pipeline_id": "pipe-inject", "session_id": "thread_inject"}),
        )
        .await
        .unwrap();

    let events = capture.events.lock().unwrap().clone();
    assert_eq!(events.len(), 1, "注入发生的 run 恒发一次消费回调");
    let records = &events[0].records;
    assert_eq!(
        records.len(),
        2,
        "逐条权威 record 必须与 cmid 一一配对（认领载荷，缺一即无 key 终态）"
    );
    assert_eq!(records[0].client_message_id, "cmid-a");
    assert_eq!(records[1].client_message_id, "cmid-b");
    assert_eq!(records[0].content, "排队消息A");
    assert_eq!(records[1].content, "排队消息B");
    assert!(
        !records[0].message_id.is_empty() && !records[1].message_id.is_empty(),
        "指纹 record_id 必带（claim 幂等键，与 REST 读路径同源）"
    );
    assert!(
        records[1].sequence > records[0].sequence,
        "FIFO 消费序必须等于权威槽位序：A={} B={}",
        records[0].sequence,
        records[1].sequence
    );
}
/// merge_and_project 对与本管道基线同值的不变键跳过更新：首写 journal 记键、
/// 轨迹落行（管道自含锚——2026-08-30 裁定零改动）；同值重写不 journal →
/// diff 空 → 不产生 trace 行（没有 update 就没有轨迹）；值变更恢复落行。
#[tokio::test]
async fn merge_and_project_skips_identical_invariant_update_no_journal_no_trace() {
    // 不 clear 全局：并发测试互清共享基线即互踩（c4689c7b9 同族）；管道
    // scope "pt-suppress" 仅本测试写入，无 residue 可防，clear 纯风险。
    let null = Arc::new(NullStorage::default());
    let store: Arc<dyn StorageBackend> = null.clone();
    let executor = PipelineExecutor::new(
        Arc::new(MockInvoker::new()) as Arc<dyn PluginInvoker>,
        PathBuf::from("."),
        TenantContext::new("t", "s"),
        Vec::<String>::new(),
        store,
        "run_suppress",
    );
    let big = json!({"text": format!("m-{}", "x".repeat(8192))});
    let mut state = json!({"pipeline_id": "pt-suppress"});
    let mut updates = HashMap::new();
    updates.insert("system_message".to_string(), big.clone());

    // 首写：journal 记键 + 轨迹落行（管道自含锚）
    executor
        .merge_and_project(&mut state, &updates)
        .await
        .unwrap();
    assert!(
        executor
            .step_key_journal
            .lock()
            .contains_key("system_message"),
        "首写照常 journal"
    );
    executor
        .append_window_trace("run_init", &state)
        .await
        .unwrap();
    assert_eq!(
        null.traces.lock().unwrap()[0].patch_data["system_message"],
        big,
        "管道首写照迹自含全值"
    );

    // 新窗口同值重写：跳过更新 → journal 无键 → diff 空 → 不产生 trace 行
    executor.step_key_journal.lock().clear();
    executor
        .merge_and_project(&mut state, &updates)
        .await
        .unwrap();
    assert!(
        !executor
            .step_key_journal
            .lock()
            .contains_key("system_message"),
        "同值重写不 journal（没有 update）"
    );
    executor
        .append_window_trace("run_init", &state)
        .await
        .unwrap();
    assert_eq!(
        null.traces.lock().unwrap().len(),
        1,
        "同值重写不产生新 trace 行"
    );

    // 值变更：恢复 journal + 落行（同值跳过永不吞真变更）
    let big2 = json!({"text": format!("m2-{}", "y".repeat(8192))});
    let mut updates2 = HashMap::new();
    updates2.insert("system_message".to_string(), big2.clone());
    executor
        .merge_and_project(&mut state, &updates2)
        .await
        .unwrap();
    executor
        .append_window_trace("run_init", &state)
        .await
        .unwrap();
    {
        let traces = null.traces.lock().unwrap();
        assert_eq!(traces.len(), 2);
        assert_eq!(traces[1].patch_data["system_message"], big2, "变更落行");
    }
}

/// DB-first 判别测试（先落 DB 后动内存）：messages 表写失败 → merge_and_project
/// 重试耗尽必须上抛 Err，且内存 messages 数组零副作用、实录零残留。若实现仍是
/// 内存先行（旧语义：内存先变、表侧失败仅 warn 继续），本测试内存断言即红——
/// 内存留下新消息而 DB 没有，「内存已注入、实录缺席」分叉窗口。
#[tokio::test]
async fn merge_and_project_messages_db_write_failure_raises_and_memory_untouched() {
    let null = Arc::new(NullStorage::default());
    null.fail_messages_ops.store(true, Ordering::SeqCst);
    let store: Arc<dyn StorageBackend> = null.clone();
    let executor = PipelineExecutor::new(
        Arc::new(MockInvoker::new()) as Arc<dyn PluginInvoker>,
        PathBuf::from("."),
        TenantContext::new("t_dbfirst", "s_dbfirst"),
        Vec::<String>::new(),
        store,
        "run_dbfirst",
    );
    let mut state = json!({
        "pipeline_id": "pipe-dbfirst",
        "messages": [{"seq": 0, "role": "user", "content": "旧消息"}],
    });
    let mut updates = HashMap::new();
    updates.insert(
        "messages".to_string(),
        json!({"_ops": [{"op": "set", "msg": {"role": "assistant", "content": "新消息"}}]}),
    );

    let result = executor.merge_and_project(&mut state, &updates).await;
    assert!(
        matches!(result, Err(agentos_core::types::StorageError::Database(ref m))
            if m.contains("injected")),
        "表写失败重试耗尽必须上抛，实际 {result:?}"
    );
    // 内存零副作用（判别 DB-first：内存先行实现会留下新消息）
    let msgs = state["messages"].as_array().expect("messages 保持数组");
    assert_eq!(msgs.len(), 1, "表写失败内存不得新增消息");
    assert_eq!(msgs[0]["content"], "旧消息", "原有消息不得被改写");
    // 实录零残留（失败批不得混入 step 轨迹）
    assert!(
        executor.ops_ledger.lock().is_empty(),
        "失败批不得进 ops 实录"
    );
}
#[tokio::test]
async fn merge_and_project_invariant_suppress_skips_trace_never_state_on_fresh_run() {
    // 回归（2026-11-02 装机实证）：基线跨 run 存活 + run state 每轮重建，
    // 同值抑制若连 set 一起跳，重建 state 永远拿不到该键——tool_schemas
    // 断链 → LLM 裸请求无 tools、system_message 同丢，模型降级正文伪调用。
    // 抑制的语义边界 = 只免轨迹（journal/diff），set 必须照常落地。
    let null = Arc::new(NullStorage::default());
    let store: Arc<dyn StorageBackend> = null.clone();
    let executor = PipelineExecutor::new(
        Arc::new(MockInvoker::new()) as Arc<dyn PluginInvoker>,
        PathBuf::from("."),
        TenantContext::new("t", "s"),
        Vec::<String>::new(),
        store,
        "run_suppress_fresh",
    );
    let big =
        json!([{"function": {"name": "pim", "description": format!("d-{}", "x".repeat(8192))}}]);

    // run A：首写落 state + 登记基线
    let mut state_a = json!({"pipeline_id": "pt-suppress-fresh"});
    let mut updates = HashMap::new();
    updates.insert("tool_schemas".to_string(), big.clone());
    executor
        .merge_and_project(&mut state_a, &updates)
        .await
        .unwrap();
    assert_eq!(state_a["tool_schemas"], big, "run A 首写落 state");

    // run B：state 每轮重建（空起点），同值重写——set 必须照常落地
    let mut state_b = json!({"pipeline_id": "pt-suppress-fresh"});
    executor.step_key_journal.lock().clear();
    executor
        .merge_and_project(&mut state_b, &updates)
        .await
        .unwrap();
    assert_eq!(
        state_b["tool_schemas"], big,
        "同值抑制只免轨迹：重建 state 仍必须拿到全值"
    );
    assert!(
        !executor
            .step_key_journal
            .lock()
            .contains_key("tool_schemas"),
        "同值重写不 journal（轨迹去重语义保持）"
    );
}
