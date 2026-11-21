// @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: rust-test
//! 持久化与门控（run 起止落库 / when 门 / trace / 路由 / 边界检查）。

use super::*;
use crate::pipeline_loop::plugins::METRICS_CLASS_LLM;
use crate::pipeline_loop::plugins::METRICS_CLASS_TOOL;
#[tokio::test]
async fn record_run_start_failure_counts_persist_failure_and_run_continues() {
    // record_run_start（运行开始簿记，state 运行键）失败不得静默：与同函数
    // link_pipeline_session 同款 warn + persist_failure 计数；
    // 同时执行面不受阻（簿记失败只降级运行可查性，不中断本轮）。
    let fixture = Fixture::build(&["a"]);
    fixture
        .store
        .fail_record_run_start
        .store(true, std::sync::atomic::Ordering::SeqCst);
    fixture.invoker.set_result(
        "a",
        PluginResult {
            state_updates: updates(&[("x", json!(1))]),
            ..Default::default()
        },
    );
    let config = gated_body(vec![StepItem::Bare("a".into())]);
    let initial = json!({ "pipeline_id": "pipe-mapping-x", "session_id": "thread-mapping-x" });

    let final_state = fixture.run(&config, &StepLibrary::default(), initial).await;

    assert_eq!(
        fixture.invoker.call_count("a"),
        1,
        "归属登记失败不得中断本轮执行"
    );
    assert_eq!(final_state["x"], json!(1));
    let snap = fixture.executor.metrics().snapshot();
    assert!(
        snap.persist_failures >= 1,
        "record_run_start 失败必须计入 persist_failure（不得静默），实际 {}",
        snap.persist_failures
    );
}
#[tokio::test]
async fn conditional_loop_route_beats_fallback_end() {
    // 条件分支（flag=true）排在兜底 end 之前：命中时 loop 续跑而非 end。
    // 序列模拟插件跨轮行为：第 1 轮置位 flag，第 2 轮清除（恒置位会无限循环）。
    let counter = Arc::new(AtomicUsize::new(0));
    let invoker = Arc::new(SequenceInvoker {
        counter: counter.clone(),
        results: vec![
            updates(&[("flag", json!(true))]),
            updates(&[("flag", json!(false))]),
        ],
    });
    let executor = make_executor(invoker.clone() as Arc<dyn PluginInvoker>, &["p"]);
    let config = routed_loop_body(vec!["p".into()]);
    let state = executor
        .run_compiled(
            &compile_pipeline(&config, &StepLibrary::default(), &executor.plugin_ids)
                .expect("compile should succeed"),
            json!({}),
        )
        .await
        .unwrap();
    // 条件分支命中 → 续跑一轮；标志清除后兜底 end
    assert_eq!(counter.load(Ordering::SeqCst), 2);
    assert_eq!(state["ended"], json!(true));
    assert_eq!(state["core_type"], json!("llm_call"));
}
#[tokio::test]
async fn fallback_end_when_condition_never_matches() {
    // 条件分支未命中（flag=false）→ 兜底 end，不无限循环。
    let fixture = Fixture::build(&["p"]);
    fixture.invoker.set_result(
        "p",
        PluginResult {
            state_updates: updates(&[("flag", json!(false))]),
            ..Default::default()
        },
    );
    let config = routed_loop_body(vec!["p".into()]);
    let state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(fixture.invoker.call_count("p"), 1);
    assert_eq!(state["ended"], json!(true));
}
#[test]
fn clear_transient_for_ops_clears_chunk_and_binding() {
    let reg = crate::transient::global_registry();
    let pipe = "pipe_clear_ops";
    reg.set(
        "tenant_test",
        pipe,
        "chunk:mid_stream",
        json!({"text_len": 3}),
    );
    reg.set(
        "tenant_test",
        pipe,
        "chunk:mid_plugin",
        json!({"text_len": 5}),
    );
    reg.set("tenant_test", pipe, "progress:1", json!({"pct": 10}));
    reg.register_message_binding("tenant_test", pipe, "mid_stream", "core");
    reg.register_message_binding("tenant_test", pipe, "mid_plugin", "core");

    let executor = Fixture::build(&["a"]).executor;
    let state = json!({
        "pipeline_id": pipe,
        "message_id": "mid_stream",
        "messages": [],
    });
    let ops = vec![
        // 无 msg.id 的 op（A1 注入路径：message_id 在 state 顶层）
        json!({"op": "set", "seq": 1, "msg": {"role": "assistant", "content": "x"}}),
        // 插件路径：消息自带 id（p_ 命名空间）
        json!({"op": "set", "seq": 2, "msg": {"role": "assistant", "content": "y", "id": "mid_plugin"}}),
        // 未知 op 形态（无 id 可提）不 panic
        json!({"op": "bogus"}),
    ];
    executor.clear_transient_for_ops(&state, &ops, "tenant_test");

    // state["message_id"] 对应键 + 消息自带 id 对应键均清
    assert!(reg.get("tenant_test", pipe, "chunk:mid_stream").is_none());
    assert!(reg.get("tenant_test", pipe, "chunk:mid_plugin").is_none());
    // B 区同清
    assert!(reg
        .resolve_step_of("tenant_test", pipe, "mid_stream")
        .is_none());
    assert!(reg
        .resolve_step_of("tenant_test", pipe, "mid_plugin")
        .is_none());
    // 非消息键不受影响（progress 中间态存活）
    assert!(reg.get("tenant_test", pipe, "progress:1").is_some());
    reg.clear_pipeline("tenant_test", pipe);
}
#[test]
fn clear_transient_for_ops_skips_without_pipeline_id() {
    let reg = crate::transient::global_registry();
    let pipe = "pipe_skip_ops";
    reg.set("tenant_test", pipe, "chunk:mid", json!({"text_len": 3}));
    let executor = Fixture::build(&["a"]).executor;
    // state 无 pipeline_id（首轮未注入场景）→ 零操作，不误清其他管道
    executor.clear_transient_for_ops(&json!({"message_id": "mid"}), &[], "tenant_test");
    assert!(reg.get("tenant_test", pipe, "chunk:mid").is_some());
    reg.clear_pipeline("tenant_test", pipe);
}
#[test]
fn bind_step_message_registers_and_guard_clears_on_drop() {
    let reg = crate::transient::global_registry();
    let pipe = "pipe_bind_guard";
    let executor = Fixture::build(&["a"]).executor;
    let state = json!({"pipeline_id": pipe, "message_id": "m1"});
    {
        let _guard = executor.bind_step_message(&state, "core").expect("应登记");
        assert_eq!(
            reg.resolve_step_of("tenant_test", pipe, "m1").as_deref(),
            Some("core")
        );
        // 守卫在作用域内持活：绑定保留
    }
    // Drop：绑定清除
    assert!(reg.resolve_step_of("tenant_test", pipe, "m1").is_none());
    reg.clear_pipeline("tenant_test", pipe);
}
#[test]
fn bind_step_message_none_when_identifiers_missing() {
    let executor = Fixture::build(&["a"]).executor;
    assert!(
        executor.bind_step_message(&json!({}), "core").is_none(),
        "无 id 零操作"
    );
    assert!(
        executor
            .bind_step_message(&json!({"pipeline_id": "p"}), "core")
            .is_none(),
        "缺 message_id 零操作"
    );
}
#[test]
fn step_binding_guard_nested_override_restores_outer() {
    let reg = crate::transient::global_registry();
    let pipe = "pipe_bind_nested";
    let executor = Fixture::build(&["a"]).executor;
    let state = json!({"pipeline_id": pipe, "message_id": "m1"});
    let outer = executor.bind_step_message(&state, "outer").unwrap();
    // 嵌套 step 覆盖登记（composite 递归：内层 step 流式窗口接管归属）
    let inner = executor.bind_step_message(&state, "inner").unwrap();
    assert_eq!(
        reg.resolve_step_of("tenant_test", pipe, "m1").as_deref(),
        Some("inner")
    );
    // 内层守卫收尾：归属恢复为外层 step（流式窗口关闭，执行权回到外层剩余项）
    drop(inner);
    assert_eq!(
        reg.resolve_step_of("tenant_test", pipe, "m1").as_deref(),
        Some("outer"),
        "内层守卫收尾须恢复外层绑定"
    );
    // 外层守卫收尾：首次登记（无 prev）→ 直接清除
    drop(outer);
    assert!(
        reg.resolve_step_of("tenant_test", pipe, "m1").is_none(),
        "外层守卫收尾清除绑定"
    );
    reg.clear_pipeline("tenant_test", pipe);
}
/// 具名步骤服务端到端：编译产物携带 method（步骤名）→ 运行期 config 含
/// `_step_method`（SDK 侧按名分发）→ state_updates 正常 merge。
#[tokio::test]
async fn named_step_service_reaches_plugin_via_step_method_config() {
    let fixture = Fixture::build(&["task_service"]);
    fixture.invoker.set_result(
        "task_service",
        PluginResult {
            state_updates: updates(&[("injected", json!(true))]),
            ..Default::default()
        },
    );
    // 复合体显式声明 steps：task.inject_params 是注册步骤名（非插件 id）
    let composite = {
        let mut m = test_manifest("task_service");
        m.capabilities = ManifestCapabilities {
            steps: vec![StepCapability {
                name: "task.inject_params".into(),
                description: None,
                input_schema: None,
            }],
            tools: vec![tool_cap("task_submit")],
            ..Default::default()
        };
        m
    };
    let index = crate::compiler::build_step_service_index(&[composite]).expect("index build ok");
    let config = PipelineConfig {
        name: "p".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![atomic_step("s", "task.inject_params")],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let compiled = crate::compiler::compile_pipeline_with_hooks(
        &config,
        &StepLibrary::default(),
        &fixture.executor.plugin_ids,
        Some(&index),
        &[],
        &[],
    )
    .expect("具名步骤服务命中编译通过");
    let final_state = fixture
        .executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("run ok");
    // 编译产物 method 携带 + 运行期 config 注入 _step_method（无 inputs → 仅约定键）
    assert_eq!(
        fixture.invoker.captured_configs("task_service"),
        vec![json!({ "_step_method": "task.inject_params" })],
        "具名步骤服务调用 config 必须携带 _step_method"
    );
    assert_eq!(
        final_state["injected"],
        json!(true),
        "具名步骤 state_updates 正常 merge"
    );
}
/// 复合体直引（插件 id 未在 capabilities.steps 声明）→ 编译期报错（fail-closed，
/// 强制自白条款）；错误文案含插件 id 与修复指引。
#[tokio::test]
async fn composite_direct_reference_fails_compilation() {
    let fixture = Fixture::build(&["task_service"]);
    let composite = {
        let mut m = test_manifest("task_service");
        m.capabilities = ManifestCapabilities {
            tools: vec![tool_cap("task_submit")],
            ..Default::default()
        };
        m
    };
    let index = crate::compiler::build_step_service_index(&[composite]).expect("index build ok");
    let config = PipelineConfig {
        name: "p".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![atomic_step("s", "task_service")],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let err = crate::compiler::compile_pipeline_with_hooks(
        &config,
        &StepLibrary::default(),
        &fixture.executor.plugin_ids,
        Some(&index),
        &[],
        &[],
    )
    .expect_err("复合体直引必须编译期报错");
    assert!(err.message.contains("task_service"), "err: {err}");
    assert!(err.message.contains("capabilities.steps"), "err: {err}");
}
/// 隐式直引（单入口插件，method None = 默认 execute 入口）：config 不带
/// `_step_method` 键——SDK 侧走现行 execute 路径（现状零改动）。
#[tokio::test]
async fn implicit_direct_reference_config_has_no_step_method_key() {
    let fixture = Fixture::build(&["a"]);
    let config = gated_body(vec![StepItem::Bare("a".into())]);
    fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    let captured = fixture.invoker.captured_configs("a");
    assert_eq!(captured.len(), 1, "隐式直引应恰好调用一次");
    assert!(
        captured[0].get("_step_method").is_none(),
        "隐式直引不得携带 _step_method，实际: {}",
        captured[0]
    );
}
/// pre 过滤（≥2 组有区分度输入）：pre:<步> 的 when 谓词为假 → run 动作零效果
/// （触发步零调用）；为真 → 触发步先于主步执行。
#[tokio::test]
async fn check_pre_when_gate_filters_dispatch() {
    // 组 A：gate=open → when 真 → 触发步（relay→runner 插件）执行，主步照常
    let fixture = Fixture::build(&["a", "runner"]);
    let config = PipelineConfig {
        name: "p".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![atomic_step("s1", "a"), atomic_step("relay", "runner")],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let compiled = compiled_with_checks(
        &fixture,
        &config,
        &[(
            "main".into(),
            vec![check("pre:s1", Some("gate == 'open'"), "run:relay")],
        )],
        &[],
    );
    let final_state = fixture
        .executor
        .run_compiled(&compiled, json!({"gate": "open"}))
        .await
        .expect("run ok");
    let configs = fixture.invoker.captured_configs("runner");
    assert_eq!(
        fixture.invoker.call_count("runner"),
        2,
        "when 真 → 触发一次 + relay 步流内顺序执行一次: configs={configs:?}"
    );
    assert!(
        configs[0].get("_pipe_check").is_some() && configs[1].get("_pipe_check").is_none(),
        "首调 = check 触发（带 _pipe_check），次调 = 流内顺序执行: {configs:?}"
    );
    assert_eq!(fixture.invoker.call_count("a"), 1, "主步照常执行");
    assert_eq!(
        final_state.get("ended"),
        Some(&json!(false)),
        "run 动作不改变流程"
    );
    // 组 B：gate=closed → when 假 → 零触发（唯一调用 = 流内顺序执行，无 _pipe_check）
    let fixture = Fixture::build(&["a", "runner"]);
    let compiled = compiled_with_checks(
        &fixture,
        &config,
        &[(
            "main".into(),
            vec![check("pre:s1", Some("gate == 'open'"), "run:relay")],
        )],
        &[],
    );
    fixture
        .executor
        .run_compiled(&compiled, json!({"gate": "closed"}))
        .await
        .expect("run ok");
    assert_eq!(
        fixture.invoker.call_count("runner"),
        1,
        "when 假 → 零触发（仅剩流内顺序执行）"
    );
    let configs = fixture.invoker.captured_configs("runner");
    assert!(
        configs.iter().all(|c| c.get("_pipe_check").is_none()),
        "零触发 = 无 check 触发调用: {configs:?}"
    );
    assert_eq!(fixture.invoker.call_count("a"), 1, "主步不受影响");
}
/// post terminate（≥2 组有区分度输入）：post:<步> 动作 terminate → ended=true
/// 短路本循环体（后续步不再执行）；对照组（when 恒假）→ 后续步照常执行。
#[tokio::test]
async fn check_post_terminate_ends_loop_body() {
    // 组 A：terminate 触发 → s2 截断
    let fixture = Fixture::build(&["a", "b"]);
    let config = two_step_config();
    let compiled = compiled_with_checks(
        &fixture,
        &config,
        &[("main".into(), vec![check("post:s1", None, "terminate")])],
        &[],
    );
    let final_state = fixture
        .executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("run ok");
    assert_eq!(final_state["ended"], json!(true), "terminate → ended=true");
    assert_eq!(fixture.invoker.call_count("a"), 1, "s1 执行");
    assert_eq!(fixture.invoker.call_count("b"), 0, "s2 被 terminate 截断");
    // 边界写归属：terminate 的 ended 写落 check@post:s1 来源标注窗口轨迹
    assert!(
        fixture
            .store
            .trace_plugin_ids
            .lock()
            .unwrap()
            .iter()
            .any(|id| id == "check@post:s1"),
        "terminate 写必须有 check@post:s1 来源标注窗口轨迹: {:?}",
        fixture.store.trace_plugin_ids.lock().unwrap()
    );
    // 组 B：同边界 when 恒假 → 不触发 → s2 照常
    let fixture = Fixture::build(&["a", "b"]);
    let compiled = compiled_with_checks(
        &fixture,
        &config,
        &[(
            "main".into(),
            vec![check("post:s1", Some("never == 'set'"), "terminate")],
        )],
        &[],
    );
    let final_state = fixture
        .executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("run ok");
    assert_eq!(
        final_state.get("ended"),
        Some(&json!(false)),
        "未触发不置 ended"
    );
    assert_eq!(fixture.invoker.call_count("b"), 1, "s2 照常执行");
}
/// on-state route（≥2 组有区分度输入）：state:<键> 订阅——写键后首个步边界
/// 消费，route 动作转移控制流（目标步执行、被跳过的步零调用）；when equals
/// 值条件为假 → 不转移。
#[tokio::test]
async fn check_on_state_route_transfers_control() {
    // 组 A：写 flag=done → on-state route:route_target 转移（目标执行、tail 跳过）
    let fixture = Fixture::build(&["writer", "skipme", "target", "tail"]);
    fixture.invoker.set_result(
        "writer",
        PluginResult {
            state_updates: updates(&[("flag", json!("done"))]),
            ..Default::default()
        },
    );
    let config = PipelineConfig {
        name: "p".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![
                atomic_step("write", "writer"),
                atomic_step("skipped", "skipme"),
                atomic_step("route_target", "target"),
                atomic_step("aftermath", "tail"),
            ],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let compiled = compiled_with_checks(
        &fixture,
        &config,
        &[(
            "main".into(),
            vec![check("state:flag", None, "route:route_target")],
        )],
        &[],
    );
    fixture
        .executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("run ok");
    assert_eq!(fixture.invoker.call_count("writer"), 1, "写步执行");
    assert_eq!(
        fixture.invoker.call_count("skipme"),
        0,
        "route 转移跳过的步零调用"
    );
    assert_eq!(fixture.invoker.call_count("target"), 1, "route 目标步执行");
    assert_eq!(
        fixture.invoker.call_count("tail"),
        1,
        "转移后流继续顺序推进（目标之后的步照常）"
    );
    // 组 B：订阅键未被写 → 零触发，顺序推进
    let fixture = Fixture::build(&["writer", "skipme", "target", "tail"]);
    let compiled = compiled_with_checks(
        &fixture,
        &config,
        &[(
            "main".into(),
            vec![check("state:flag", None, "route:route_target")],
        )],
        &[],
    );
    fixture
        .executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("run ok");
    assert_eq!(
        fixture.invoker.call_count("target"),
        1,
        "未写键零触发（target 为流内顺序执行）"
    );
    assert_eq!(fixture.invoker.call_count("skipme"), 1, "零触发不跳步");
    assert_eq!(fixture.invoker.call_count("tail"), 1, "顺序推进不受影响");
    // 组 C：when equals 值条件为假（写的值不匹配）→ 不转移
    let fixture = Fixture::build(&["writer", "skipme", "target", "tail"]);
    fixture.invoker.set_result(
        "writer",
        PluginResult {
            state_updates: updates(&[("flag", json!("done"))]),
            ..Default::default()
        },
    );
    let compiled = compiled_with_checks(
        &fixture,
        &config,
        &[(
            "main".into(),
            vec![check(
                "state:flag",
                Some("flag == 'pending'"),
                "route:route_target",
            )],
        )],
        &[],
    );
    fixture
        .executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("run ok");
    assert_eq!(
        fixture.invoker.call_count("target"),
        1,
        "equals 条件为假不转移（target 为流内顺序执行）"
    );
    assert_eq!(fixture.invoker.call_count("skipme"), 1, "不转移不跳步");
    assert_eq!(fixture.invoker.call_count("tail"), 1);
}
/// 一次性触发步：触发一次执行一次（与顺序步同语义），轨迹身份 = 模板步 id +
/// 触发纪元（多次触发多条目不混叠）；触发上下文经 `_pipe_check` 透传。
#[tokio::test]
async fn check_run_step_executes_once_with_epoch_trace() {
    let fixture = Fixture::build(&["a", "b", "runner"]);
    // runner 按调用序写不同的值 → 每次触发的轨迹窗口都有净变更（无产出步不落
    // 轨迹的既有语义不变，轨迹身份断言依赖窗口产出）
    fixture.invoker.set_sequence(
        "runner",
        vec![
            PluginResult {
                state_updates: updates(&[("relayed", json!(1))]),
                ..Default::default()
            },
            PluginResult {
                state_updates: updates(&[("relayed", json!(2))]),
                ..Default::default()
            },
        ],
    );
    let config = PipelineConfig {
        name: "p".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![
                atomic_step("s1", "a"),
                atomic_step("s2", "b"),
                atomic_step("relay", "runner"),
            ],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    // pre:s1 与 post:s1 各触发一次 run:relay → 单轮内两次触发、两个纪元
    let compiled = compiled_with_checks(
        &fixture,
        &config,
        &[(
            "main".into(),
            vec![
                check("pre:s1", None, "run:relay"),
                check("post:s1", None, "run:relay"),
            ],
        )],
        &[],
    );
    let final_state = fixture
        .executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("run ok");
    assert_eq!(
        fixture.invoker.call_count("runner"),
        3,
        "两次触发各执行一次 + relay 步流内顺序执行一次"
    );
    assert_eq!(fixture.invoker.call_count("a"), 1, "主步 s1 照常执行");
    assert_eq!(
        fixture.invoker.call_count("b"),
        1,
        "后续步 s2 照常（run 不转移）"
    );
    assert_eq!(
        final_state.get("ended"),
        Some(&json!(false)),
        "run 不终止流程"
    );
    // 轨迹身份 = 模板步 id + 触发纪元：relay#1 / relay#2 两条独立轨迹条目
    let trace_ids = fixture.store.trace_plugin_ids.lock().unwrap().clone();
    assert!(
        trace_ids.iter().any(|id| id == "relay#1"),
        "触发纪元 #1 轨迹条目: {trace_ids:?}"
    );
    assert!(
        trace_ids.iter().any(|id| id == "relay#2"),
        "触发纪元 #2 轨迹条目（多条目不混叠）: {trace_ids:?}"
    );
    // 触发上下文 `_pipe_check`：at + 触发纪元随本步插件调用透传（流内顺序
    // 执行的那次不带 _pipe_check）
    let configs = fixture.invoker.captured_configs("runner");
    assert!(
        configs
            .iter()
            .any(|c| c["_pipe_check"]["at"] == "pre:s1" && c["_pipe_check"]["epoch"] == 1),
        "pre 触发上下文透传: {configs:?}"
    );
    assert!(
        configs
            .iter()
            .any(|c| c["_pipe_check"]["at"] == "post:s1" && c["_pipe_check"]["epoch"] == 2),
        "post 触发上下文透传: {configs:?}"
    );
    assert_eq!(
        configs
            .iter()
            .filter(|c| c.get("_pipe_check").is_some())
            .count(),
        2,
        "恰好两次触发调用携带 _pipe_check（顺序执行不携带）: {configs:?}"
    );
}
/// run 步自身产出 Step 跳转：触发边界无跳转宿主，跳转不消费（主流程继续
/// 顺序推进），warn 留痕。
#[tokio::test]
async fn check_run_step_self_jump_is_not_consumed() {
    let fixture = Fixture::build(&["a", "runner", "skipped_target"]);
    // relay 步自身路由：when True → Step 跳转 "relay_skip"（编译期校验合法）
    let mut relay = atomic_step("relay", "runner");
    relay.routes = vec![agentos_core::types::Route {
        when: "True".into(),
        then: agentos_core::types::RouteAction {
            next: RouteNext::Step("relay_skip".into()),
            set: HashMap::new(),
        },
    }];
    let config = PipelineConfig {
        name: "p".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![
                atomic_step("s1", "a"),
                relay,
                atomic_step("relay_skip", "skipped_target"),
            ],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let compiled = compiled_with_checks(
        &fixture,
        &config,
        &[("main".into(), vec![check("pre:s1", None, "run:relay")])],
        &[],
    );
    fixture
        .executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("run ok");
    // run 步触发执行了（1 次），但其自身 Step 跳转不消费——relay_skip 只在
    // 流内顺序位置执行一次（若无跳转消费则同样 1 次；区别在于顺序流未被
    // run 触发改写——skipped_target 的调用全部来自顺序位置）
    assert_eq!(
        fixture.invoker.call_count("runner"),
        2,
        "触发一次 + 流内一次"
    );
    assert_eq!(
        fixture.invoker.call_count("skipped_target"),
        1,
        "run 触发的跳转不消费，主流程顺序推进照旧"
    );
    assert_eq!(fixture.invoker.call_count("a"), 1, "主步照常执行");
}
/// state_delta 键级事件：写路径 journal 落点发射，on-state run 动作的触发步
/// 经 `_pipe_check.state_delta` 收到键级 payload（key/old/new）。
#[tokio::test]
async fn state_delta_emission_carries_key_payload() {
    // 组 A：写键 flag=done（原值 absent）→ on-state 触发步收到键级 payload
    let fixture = Fixture::build(&["writer", "reporter"]);
    fixture.invoker.set_result(
        "writer",
        PluginResult {
            state_updates: updates(&[("flag", json!("done"))]),
            ..Default::default()
        },
    );
    let config = PipelineConfig {
        name: "p".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![
                atomic_step("write", "writer"),
                atomic_step("report_step", "reporter"),
            ],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let compiled = compiled_with_checks(
        &fixture,
        &config,
        &[(
            "main".into(),
            vec![check("state:flag", None, "run:report_step")],
        )],
        &[],
    );
    fixture
        .executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("run ok");
    // 触发一次（report_step 步也在流内顺序执行一次，由 _pipe_check 区分）
    assert_eq!(
        fixture.invoker.call_count("reporter"),
        2,
        "写键 → 触发一次 + 流内顺序执行一次"
    );
    let configs = fixture.invoker.captured_configs("reporter");
    let triggered = configs
        .iter()
        .find(|c| c.get("_pipe_check").is_some())
        .expect("触发调用带 _pipe_check");
    let delta = &triggered["_pipe_check"]["state_delta"][0];
    assert_eq!(delta["key"], json!("flag"), "payload 携带键名");
    assert_eq!(delta["new"], json!("done"), "payload 携带新值");
    assert_eq!(delta["old"], serde_json::Value::Null, "新增键旧值为 null");
    // 组 B：写的键无订阅 → 零触发（事件不残留、不误触发）
    let fixture = Fixture::build(&["writer", "reporter"]);
    let compiled = compiled_with_checks(
        &fixture,
        &config,
        &[(
            "main".into(),
            vec![check("state:other", None, "run:report_step")],
        )],
        &[],
    );
    fixture
        .executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("run ok");
    assert_eq!(
        fixture.invoker.call_count("reporter"),
        1,
        "未订阅键的写不触发（仅剩流内顺序执行）"
    );
    let configs = fixture.invoker.captured_configs("reporter");
    assert!(
        configs.iter().all(|c| c.get("_pipe_check").is_none()),
        "零触发 = 无 check 触发调用"
    );
    assert_eq!(fixture.invoker.call_count("writer"), 1);
}
/// 护栏①（步产出路由优先）：post 边界步已产出路由（Step 跳转）时 check route
/// 抑制——步自身的跳转目标执行，check 目标被跳过。
#[tokio::test]
async fn step_produced_route_takes_priority_over_check_route() {
    let fixture = Fixture::build(&["a", "check_target", "step_target"]);
    let mut s1 = atomic_step("s1", "a");
    s1.routes = vec![agentos_core::types::Route {
        when: "True".into(),
        then: agentos_core::types::RouteAction {
            next: RouteNext::Step("s3".into()),
            set: HashMap::new(),
        },
    }];
    let config = PipelineConfig {
        name: "p".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![
                s1,
                atomic_step("s2", "check_target"),
                atomic_step("s3", "step_target"),
            ],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let compiled = compiled_with_checks(
        &fixture,
        &config,
        &[("main".into(), vec![check("post:s1", None, "route:s2")])],
        &[],
    );
    fixture
        .executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("run ok");
    assert_eq!(
        fixture.invoker.call_count("step_target"),
        1,
        "步产出路由优先消费（s3 执行）"
    );
    assert_eq!(
        fixture.invoker.call_count("check_target"),
        0,
        "check route 抑制（s2 零调用）"
    );
}
/// 空表零分发：未声明任何 checks 的管道，边界分发点零开销。
#[tokio::test]
async fn no_checks_yields_zero_dispatch() {
    let fixture = Fixture::build(&["a", "runner"]);
    let config = PipelineConfig {
        name: "p".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![atomic_step("s1", "a")],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let compiled = compile_pipeline(
        &config,
        &StepLibrary::default(),
        fixture.executor.plugin_ids(),
    )
    .expect("compile ok");
    fixture
        .executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("run ok");
    assert_eq!(fixture.invoker.call_count("a"), 1, "主流程不受影响");
    // 无 checks → 无一次性触发步、无 _pipe_check 注入
    let configs = fixture.invoker.captured_configs("a");
    assert!(
        configs.iter().all(|c| c.get("_pipe_check").is_none()),
        "顺序执行零触发上下文注入"
    );
}
#[tokio::test]
async fn should_stop_breaks_loop_and_normalizes_ended() {
    // 插件只写 state：should_stop=true 在轮边界折算为 ended——循环终止、
    // 终态收束走既有 ended 语义。写方未带署名时按未署名兜底映射。
    let fixture = Fixture::build(&["a"]);
    fixture.invoker.set_result(
        "a",
        PluginResult {
            state_updates: updates(&[("should_stop", json!(true))]),
            ..Default::default()
        },
    );
    let config = always_loop_body("a");
    let final_state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(
        fixture.invoker.call_count("a"),
        1,
        "should_stop 当轮折算终止，第 2 轮不再进入"
    );
    assert_eq!(final_state["ended"], json!(true), "折算为 ended 落 state");
    assert_eq!(
        fixture.store.recorded_run_statuses(),
        vec![RunStatus::Completed],
        "未署名的终止请求按缺省映射兜底"
    );
}
#[tokio::test]
async fn stop_reason_signature_maps_run_status() {
    // 终态映射表：谁写终止谁署名（router.stop_reason），引擎只查表——
    // 预算超线署名 budget_exhausted 落 Failed，不再被 user_requested 误标。
    let cases: Vec<(&str, RunStatus)> = vec![
        ("budget_exhausted", RunStatus::Failed),
        ("elapsed_cap", RunStatus::Failed),
        ("timeout", RunStatus::Failed),
        ("task_failed", RunStatus::Failed),
        ("user_requested", RunStatus::Cancelled),
        ("task_cancelled", RunStatus::Cancelled),
        ("task_completed", RunStatus::Completed),
    ];
    for (reason, expected) in cases {
        let fixture = Fixture::build(&["a"]);
        fixture.invoker.set_result(
            "a",
            PluginResult {
                state_updates: updates(&[
                    ("should_stop", json!(true)),
                    ("router.stop_reason", json!(reason)),
                ]),
                ..Default::default()
            },
        );
        let config = always_loop_body("a");
        fixture
            .run(&config, &StepLibrary::default(), json!({}))
            .await;
        assert_eq!(
            fixture.store.recorded_run_statuses(),
            vec![expected.clone()],
            "stop_reason={reason} 应映射 {expected:?}"
        );
    }
}
#[tokio::test]
async fn suspended_still_skips_exit_body_and_maps_suspended() {
    // 挂起优先于折算：suspended=true 时整个 run 停止推进、不跑收尾体，
    // 终态落 Suspended（等待恢复）。
    let fixture = Fixture::build(&["a", "exit"]);
    fixture.invoker.set_result(
        "a",
        PluginResult {
            state_updates: updates(&[
                ("should_stop", json!(true)),
                ("suspended", json!(true)),
                ("router.stop_reason", json!("budget_exhausted")),
            ]),
            ..Default::default()
        },
    );
    let mut config = always_loop_body("a");
    config.loop_bodies.push(LoopBody {
        id: "exit".into(),
        steps: vec![atomic_step("exit_finalize", "exit")],
        while_cond: None,
        exit_routes: vec![],
        run_on_error: true,
    });
    let final_state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(
        fixture.invoker.call_count("exit"),
        0,
        "挂起不跑收尾体（环境保持供恢复）"
    );
    assert_eq!(
        fixture.store.recorded_run_statuses(),
        vec![RunStatus::Suspended]
    );
    assert_eq!(final_state["should_stop"], json!(true));
}
#[tokio::test]
async fn run_started_at_written_per_run() {
    // 耗时锚点：run 初始化写 run_started_at（RFC3339），二次 run 覆盖——
    // track 插件的 elapsed 由此计算，不再继承宿主进程 uptime。
    let fixture = Fixture::build(&["a"]);
    fixture.invoker.set_result(
        "a",
        PluginResult {
            state_updates: updates(&[("ended", json!(true))]),
            ..Default::default()
        },
    );
    let config = always_loop_body("a");
    let first = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    let second = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    for state in [&first, &second] {
        let raw = state["run_started_at"]
            .as_str()
            .expect("run_started_at 存在");
        chrono::DateTime::parse_from_rfc3339(raw).expect("RFC3339 可解析");
    }
}
#[tokio::test]
async fn run_status_terminal_written_from_expected_control_keys() {
    // 预期层插件写 ended=true → 引擎收尾按终态映射单点折算 run_status=completed；
    // state 快照与 runs 表（recorded statuses）同词汇同源。
    let fixture = Fixture::build(&["a"]);
    fixture.invoker.set_result(
        "a",
        PluginResult {
            state_updates: updates(&[("ended", json!(true))]),
            ..Default::default()
        },
    );
    let config = gated_body(vec![StepItem::Bare("a".into())]);
    let final_state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(final_state["run_status"], json!("completed"));
    assert_eq!(
        fixture.store.recorded_run_statuses(),
        vec![RunStatus::Completed],
        "state 实际状态与 runs 表同源同词汇"
    );
}
#[tokio::test]
async fn run_status_reflects_suspension() {
    // 预期层 suspended=true → 实际状态 suspended（挂起在终态映射中优先级最高）
    let fixture = Fixture::build(&["a"]);
    fixture.invoker.set_result(
        "a",
        PluginResult {
            state_updates: updates(&[("suspended", json!(true))]),
            ..Default::default()
        },
    );
    let config = gated_body(vec![StepItem::Bare("a".into())]);
    let final_state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(final_state["run_status"], json!("suspended"));
    assert_eq!(
        fixture.store.recorded_run_statuses(),
        vec![RunStatus::Suspended]
    );
}
#[tokio::test]
async fn run_status_plugin_forgery_dropped_by_terminal_mapping() {
    // 实际状态不可被插件改写：插件伪造 run_status=failed，收束仍由引擎按
    // 控制键折算（ended=true → completed）——伪造值不得存活到终态。
    let fixture = Fixture::build(&["a"]);
    fixture.invoker.set_result(
        "a",
        PluginResult {
            state_updates: updates(&[("run_status", json!("failed")), ("ended", json!(true))]),
            ..Default::default()
        },
    );
    let config = gated_body(vec![StepItem::Bare("a".into())]);
    let final_state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(
        final_state["run_status"],
        json!("completed"),
        "实际状态只出自终态映射单点，插件伪造值不得落地"
    );
}
#[tokio::test]
async fn merge_drops_run_status_plugin_write() {
    // merge 层拦截（内核持有不变量）：插件直写 run_status 即刻丢弃，state
    // 保持内核写入值；非内核键（ended）照常合并。
    let fixture = Fixture::build(&["a"]);
    let mut state = json!({"run_status": "running"});
    fixture
        .executor
        .merge_and_project(&mut state, &updates(&[("run_status", json!("failed"))]))
        .await
        .unwrap();
    assert_eq!(
        state["run_status"],
        json!("running"),
        "插件直写 run_status 必须被丢弃"
    );
    fixture
        .executor
        .merge_and_project(&mut state, &updates(&[("ended", json!(true))]))
        .await
        .unwrap();
    assert_eq!(state["ended"], json!(true), "预期层控制键照常合并");
}
/// checkpoint 剥离集 = 内核自有键（store 内置）∪ 插件声明键（manifest
/// `state.volatile_keys` 声明并集，经 StorageBackend::set_declared_volatile_keys
/// 注入 store），与收窄前内核单表全集行为逐键对账：两类键都不得残留进快照，
/// 持久键照常落档。
#[tokio::test]
async fn run_end_checkpoint_strips_declared_volatile_keys() {
    let sqlite = Arc::new(SqliteStore::open_memory().unwrap());
    // 声明键并集注入（生产侧 api 在 stage_execute 从 manifest 声明收集后传入）。
    sqlite.set_declared_volatile_keys(&[
        "thinking_strength".to_string(),
        "tool_schemas".to_string(),
        "conversation_mode".to_string(),
        "core_type".to_string(),
        "core_plugin".to_string(),
    ]);
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let invoker: Arc<dyn PluginInvoker> = Arc::new(MockInvoker::new());
    let executor = PipelineExecutor::new(
        invoker,
        PathBuf::from("."),
        TenantContext::new("default", "s"),
        std::iter::empty::<String>(),
        store,
        "r_decl",
    );
    let final_state = json!({
        "pipeline_id": "pipe_decl",
        "task.status": "running",
        // 内核自有键（store 内置剥离）
        "ended": true,
        "suspended": true,
        "run_id": "old-run",
        "should_stop": true,
        "router.stop_reason": "budget_exhausted",
        // 插件声明键（executor 注入剥离）
        "conversation_mode": true,
        "tool_schemas": [{"name": "x"}],
        "thinking_strength": "high",
        "core_type": "tool_execute",
        "core_plugin": "custom_core_engine",
    });
    executor.persist_run_end(&final_state).await;
    let latest = sqlite
        .load_latest_checkpoint("pipe_decl", "default")
        .unwrap()
        .expect("run 收尾必落 checkpoint");
    let (_, state) = latest;
    for key in [
        "ended",
        "suspended",
        "run_id",
        "should_stop",
        "router.stop_reason",
        "conversation_mode",
        "tool_schemas",
        "thinking_strength",
        "core_type",
        "core_plugin",
    ] {
        assert!(state.get(key).is_none(), "易变键 {key} 不得残留进快照");
    }
    // 持久键照常落档（快照瘦身不吞管道累计状态）
    assert_eq!(state["task.status"], "running");
}
/// 未声明插件键不受剥离影响（声明什么剥什么，缺省零干预）：裸 store
/// 未注入声明键集，conversation_mode 照常落档。
#[tokio::test]
async fn run_end_checkpoint_keeps_keys_without_declaration() {
    let sqlite = Arc::new(SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let invoker: Arc<dyn PluginInvoker> = Arc::new(MockInvoker::new());
    let executor = PipelineExecutor::new(
        invoker,
        PathBuf::from("."),
        TenantContext::new("default", "s"),
        std::iter::empty::<String>(),
        store,
        "r_keep",
    );
    let final_state = json!({
        "pipeline_id": "pipe_keep",
        "task.status": "running",
        "conversation_mode": true,
    });
    executor.persist_run_end(&final_state).await;
    let (_, state) = sqlite
        .load_latest_checkpoint("pipe_keep", "default")
        .unwrap()
        .unwrap();
    assert_eq!(
        state["conversation_mode"], true,
        "未声明键不剥离（fail-open 仅对声明面）"
    );
    assert_eq!(state["task.status"], "running");
}
/// 迁移完整性对账（K2）：原内核内置 METRICS_PLUGIN_CLASS 表覆盖的每个
/// 插件，plugin.json `capabilities.metrics_class` 都已声明且值与表退役前
/// 一致（零迁移丢失）；且全部 manifest 的声明值均属合法词表（llm/tool）。
/// 期望值逐条显式写死（表已删除，本测试即对账锚点）：漏补声明/值漂移/
/// 非法声明值即红。
#[test]
fn test_metrics_class_declaration_migration_integrity() {
    // 期望值 = 表退役前各插件的类别（原表全集，非测试内重算）
    let expected: &[(&str, PluginMetricsClass)] = &[
        // LLM 调用面
        ("llm_service", PluginMetricsClass::Llm),
        ("pipeline_llm_core", PluginMetricsClass::Llm),
        // 工具调用面（管道工具核 + 工具插件）
        ("agentos-builtin-tools", PluginMetricsClass::Tool),
        ("bash_tool", PluginMetricsClass::Tool),
        ("human_interaction_tool", PluginMetricsClass::Tool),
        ("media_tools", PluginMetricsClass::Tool),
        ("memory_tool", PluginMetricsClass::Tool),
        ("pipeline_tool_cache", PluginMetricsClass::Tool),
        ("pipeline_tool_cache_writer", PluginMetricsClass::Tool),
        ("pipeline_tool_core", PluginMetricsClass::Tool),
        ("pipeline_tool_schema", PluginMetricsClass::Tool),
        ("pipeline_tool_schema_validator", PluginMetricsClass::Tool),
        ("project_manager", PluginMetricsClass::Tool),
        ("resource_merge_tool", PluginMetricsClass::Tool),
        ("resource_search_tool", PluginMetricsClass::Tool),
        ("simple_tools", PluginMetricsClass::Tool),
        ("task_evaluate_tool", PluginMetricsClass::Tool),
        ("task_manage_tool", PluginMetricsClass::Tool),
        ("task_submit_tool", PluginMetricsClass::Tool),
        ("trigger_setup_tool", PluginMetricsClass::Tool),
        ("web_operate_tool", PluginMetricsClass::Tool),
    ];
    let shared_root =
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../plugins/shared");
    // 扫描面与 tests/test_plugin_manifest_schema.py 同构（定点扫描不递归——
    // 拍平布局（ADR 2026-10-13-plugin-flat-layout）下插件平铺共享根一层；
    // plugins/shared 下存在循环 junction，无界遍历会挂死）
    let mut paths: Vec<std::path::PathBuf> = Vec::new();
    for entry in std::fs::read_dir(&shared_root)
        .expect("plugins/shared 必须可读")
        .flatten()
    {
        paths.push(entry.path().join("plugin.json"));
    }
    // 装载面全集：id → 声明值（None = 未声明）。用真实 serde 模型解析——
    // 模型面（deny_unknown_fields）与声明面同轮受验。
    let mut declared_by_id: HashMap<String, Option<String>> = HashMap::new();
    for path in paths {
        if !path.is_file() {
            continue;
        }
        let text = std::fs::read_to_string(&path).expect("plugin.json 必须可读");
        let manifest: PluginManifest =
            serde_json::from_str(&text).unwrap_or_else(|e| panic!("{path:?} 解析失败：{e}"));
        declared_by_id.insert(manifest.id, manifest.capabilities.metrics_class);
    }
    assert!(
        declared_by_id.len() >= 50,
        "装载面 manifest 清单异常（{} 个），扫描面可能被移动",
        declared_by_id.len()
    );
    // ① 对账：表覆盖插件逐一已声明且值一致（零迁移丢失）
    for (id, class) in expected {
        let declared = declared_by_id
            .get(*id)
            .unwrap_or_else(|| panic!("{id} 不在 plugins/shared 装载面（表覆盖插件丢失）"))
            .clone();
        let want = match class {
            PluginMetricsClass::Llm => METRICS_CLASS_LLM,
            PluginMetricsClass::Tool => METRICS_CLASS_TOOL,
            PluginMetricsClass::Other => unreachable!("Other 是缺省语义，期望表不含 Other 项"),
        };
        assert_eq!(
            declared.as_deref(),
            Some(want),
            "{id} 声明值必须与表退役前类别一致（{want:?}）"
        );
    }
    // ② 性质：全部声明值均属合法词表（任何插件、任何未来新增声明）
    for (id, declared) in &declared_by_id {
        if let Some(value) = declared {
            assert!(
                PluginMetricsClass::from_declared(value).is_some(),
                "{id} 声明了非法 metrics_class 值 {value:?}（合法值：llm/tool）"
            );
        }
    }
}
/// 声明值词表解析：合法值精确映射；Other 是缺省语义不是登记项（显式
/// "other" = 非法声明，与表时代「Other 不得显式登记」同契约）；精确
/// 匹配、大小写敏感、无空白容差。
#[test]
fn test_metrics_class_from_declared_vocabulary() {
    assert_eq!(
        PluginMetricsClass::from_declared(METRICS_CLASS_LLM),
        Some(PluginMetricsClass::Llm)
    );
    assert_eq!(
        PluginMetricsClass::from_declared(METRICS_CLASS_TOOL),
        Some(PluginMetricsClass::Tool)
    );
    for illegal in [
        "other", "Other", "LLM", "TOOL", "", "llm ", " tool", "tool\n",
    ] {
        assert_eq!(
            PluginMetricsClass::from_declared(illegal),
            None,
            "非法声明值 {illegal:?} 必须被拒绝（不进分类表）"
        );
    }
}
/// 分类行为回归（K2 声明化后同插件同类别）：经 with_metrics_classes 装配
/// 声明 → 查询结果与表时代一致；未声明 id、未装配执行器、非法声明值
/// 三种边界一律 Other 兜底（新插件零声明也进指标面，不 panic 不漏）。
#[test]
fn test_metrics_class_declarative_lookup() {
    let executor = make_executor(
        Arc::new(MockInvoker::new()),
        &["llm_service", "bash_tool", "monitoring"],
    )
    .with_metrics_classes([
        ("llm_service".to_string(), METRICS_CLASS_LLM.to_string()),
        ("bash_tool".to_string(), METRICS_CLASS_TOOL.to_string()),
        ("monitoring".to_string(), "banana".to_string()), // 非法声明 → warn + Other
    ]);
    assert_eq!(
        executor.plugin_metrics_class("llm_service"),
        PluginMetricsClass::Llm,
        "llm_service 分类须与表时代一致（Llm）"
    );
    assert_eq!(
        executor.plugin_metrics_class("bash_tool"),
        PluginMetricsClass::Tool,
        "bash_tool 分类须与表时代一致（Tool）"
    );
    assert_eq!(
        executor.plugin_metrics_class("monitoring"),
        PluginMetricsClass::Other,
        "非法声明值按 Other 计（指标少计是可观测债务，非行为故障）"
    );
    assert_eq!(
        executor.plugin_metrics_class("brand_new_plugin"),
        PluginMetricsClass::Other,
        "未声明新插件兜底 Other（大小写敏感、精确匹配）"
    );
    assert_eq!(
        executor.plugin_metrics_class("LLM_SERVICE"),
        PluginMetricsClass::Other,
        "大小写敏感：变体 id 不得命中声明"
    );
    // 未装配（测试默认/未接线）= 全 Other：声明表为空不影响执行路径
    let bare = make_executor(Arc::new(MockInvoker::new()), &["llm_service"]);
    assert_eq!(
        bare.plugin_metrics_class("llm_service"),
        PluginMetricsClass::Other,
        "未注入声明表时全 Other（分类只影响 metrics，不影响执行）"
    );
}
/// 正常完成：恰收 OnPipelineStart → OnPipelineEnd 各一次，目标与标签正确。
#[tokio::test]
async fn lifecycle_bus_run_emits_start_and_end() {
    let bus = Arc::new(HookEventBus::new(16));
    let mut rx = bus.subscribe();
    let executor = make_bus_executor(Some(bus.clone()), "run_bus_ok");
    let compiled = compile_pipeline(
        &lifecycle_bus_config(),
        &StepLibrary::default(),
        &executor.plugin_ids,
    )
    .expect("compile should succeed");
    executor
        .run_compiled(&compiled, json!({"pipeline_id": "pipe-xyz"}))
        .await
        .expect("run should succeed");

    let start = rx.recv().await.expect("start event");
    assert_eq!(
        start.hook,
        agentos_core::traits::LifecycleHook::OnPipelineStart
    );
    assert_eq!(
        start.ctx.get("pipeline_id").and_then(|v| v.as_str()),
        Some("pipe-xyz"),
        "start 事件带 pipeline_id 标签"
    );
    assert_eq!(
        start.ctx.get("run_id").and_then(|v| v.as_str()),
        Some("run_bus_ok"),
        "start 事件带 run_id 标签"
    );
    assert!(
        matches!(start.target, EventTarget::Pipeline(ref p) if p == "pipe-xyz"),
        "start 目标 = Pipeline(pipeline_id)"
    );

    let end = rx.recv().await.expect("end event");
    assert_eq!(end.hook, agentos_core::traits::LifecycleHook::OnPipelineEnd);
    assert_eq!(
        end.ctx.get("pipeline_id").and_then(|v| v.as_str()),
        Some("pipe-xyz")
    );
    assert!(
        matches!(end.target, EventTarget::Pipeline(ref p) if p == "pipe-xyz"),
        "end 目标 = Pipeline(pipeline_id)"
    );

    // 恰好两次（不多发）。
    let extra = rx.try_recv().err();
    assert!(
        matches!(
            extra,
            Some(tokio::sync::broadcast::error::TryRecvError::Empty)
        ),
        "run 只应发 start+end 两个事件"
    );
}
/// 挂起提前跳出循环体的 run 仍发 start+end（与 persist_run_end 同边界）。
#[tokio::test]
async fn lifecycle_bus_suspended_run_still_emits_both() {
    let bus = Arc::new(HookEventBus::new(16));
    let mut rx = bus.subscribe();
    let executor = make_bus_executor(Some(bus.clone()), "run_bus_susp");
    let compiled = compile_pipeline(
        &lifecycle_bus_config(),
        &StepLibrary::default(),
        &executor.plugin_ids,
    )
    .expect("compile should succeed");
    let final_state = executor
        .run_compiled(
            &compiled,
            json!({"pipeline_id": "pipe-susp", "suspended": true}),
        )
        .await
        .expect("run should succeed");
    assert_eq!(
        final_state.get("suspended").and_then(|v| v.as_bool()),
        Some(true),
        "挂起状态保留"
    );

    let start = rx.recv().await.expect("start event");
    let end = rx.recv().await.expect("end event");
    assert_eq!(
        start.hook,
        agentos_core::traits::LifecycleHook::OnPipelineStart
    );
    assert_eq!(end.hook, agentos_core::traits::LifecycleHook::OnPipelineEnd);
    assert_eq!(
        end.ctx.get("pipeline_id").and_then(|v| v.as_str()),
        Some("pipe-susp")
    );
}
/// 引擎 Err 的 run 只发 start（与点对点 end 钩子同边界；失败终态由 api 层
/// run.failed 域事件承载）。
#[tokio::test]
async fn lifecycle_bus_engine_err_emits_start_only() {
    let bus = Arc::new(HookEventBus::new(16));
    let mut rx = bus.subscribe();
    let executor = make_bus_executor(Some(bus.clone()), "run_bus_err");
    let compiled = compile_pipeline(
        &lifecycle_bus_config(),
        &StepLibrary::default(),
        &executor.plugin_ids,
    )
    .expect("compile should succeed");
    // next_phase 指向不存在的循环体 → 循环体转移护栏触发 Err。
    let result = executor
        .run_compiled(
            &compiled,
            json!({"pipeline_id": "pipe-err", "next_phase": "ghost"}),
        )
        .await;
    assert!(result.is_err(), "路由到不存在的循环体应 Err");

    let start = rx.recv().await.expect("start event");
    assert_eq!(
        start.hook,
        agentos_core::traits::LifecycleHook::OnPipelineStart
    );
    let next = rx.try_recv().err();
    assert!(
        matches!(
            next,
            Some(tokio::sync::broadcast::error::TryRecvError::Empty)
        ),
        "Err 的 run 不发 OnPipelineEnd"
    );
}
/// 未注入总线零行为破坏：run 正常完成、不 panic。
#[tokio::test]
async fn lifecycle_bus_none_wired_run_ok() {
    let executor = make_bus_executor(None, "run_bus_none");
    let compiled = compile_pipeline(
        &lifecycle_bus_config(),
        &StepLibrary::default(),
        &executor.plugin_ids,
    )
    .expect("compile should succeed");
    let final_state = executor
        .run_compiled(&compiled, json!({"pipeline_id": "pipe-none"}))
        .await
        .expect("run should succeed");
    assert!(final_state.get("pipeline_id").is_some());
}
/// 事件形态契约锚点：ctx 标签恰含 pipeline_id/run_id（审计与指标订阅者
/// 按标签关联 run 的解析契约）。
#[tokio::test]
async fn lifecycle_bus_event_tags_are_subscriber_contract() {
    let bus = Arc::new(HookEventBus::new(16));
    let mut rx = bus.subscribe();
    let executor = make_bus_executor(Some(bus.clone()), "run_bus_shape");
    let compiled = compile_pipeline(
        &lifecycle_bus_config(),
        &StepLibrary::default(),
        &executor.plugin_ids,
    )
    .expect("compile should succeed");
    executor
        .run_compiled(&compiled, json!({"pipeline_id": "pipe-shape"}))
        .await
        .expect("run should succeed");

    let ev: LifecycleEvent = rx.recv().await.expect("start event");
    let tags: Vec<String> = ev.ctx.tags().keys().cloned().collect();
    assert!(tags.contains(&"pipeline_id".to_string()));
    assert!(tags.contains(&"run_id".to_string()));
}
/// 三态增量记账：append(+new) → set 替换(new-old) → set 删除(-old)，
/// 逐步断言精确字节值（字面断言）与全量基准一致性（性质断言）。
#[tokio::test]
async fn messages_chars_incremental_append_replace_delete() {
    let fixture = Fixture::build(&[]);
    let msg_a = json!({"role": "user", "content": "你好世界"});
    let msg_b = json!({"role": "assistant", "content": "answer"});
    let msg_c = json!({"role": "user", "content": "replacement"});
    let bytes = |m: &serde_json::Value| serde_json::to_string(m).unwrap().len() as i64;
    let mut state = json!({});

    // 批 1：两个 append（无 seq，引擎分配 0/1）
    fixture
        .executor
        .merge_and_project(
            &mut state,
            &messages_update(vec![
                json!({"op": "set", "msg": msg_a}),
                json!({"op": "set", "msg": msg_b}),
            ]),
        )
        .await
        .unwrap();
    assert_eq!(
        state["track.messages_chars"],
        json!(bytes(&msg_a) + bytes(&msg_b))
    );
    assert_eq!(
        state["track.messages_chars"],
        json!(oracle_messages_chars(&state))
    );

    // 批 2：set(seq=0) 替换 A → delta = bytes(C) - bytes(A)
    fixture
        .executor
        .merge_and_project(
            &mut state,
            &messages_update(vec![json!({"op": "set", "seq": 0, "msg": msg_c})]),
        )
        .await
        .unwrap();
    assert_eq!(
        state["track.messages_chars"],
        json!(bytes(&msg_b) + bytes(&msg_c)),
        "替换后 = 幸存 B + 新 C"
    );
    assert_eq!(
        state["track.messages_chars"],
        json!(oracle_messages_chars(&state))
    );

    // 批 3：set(seq=0, null) 删除 C → delta = -bytes(C)
    fixture
        .executor
        .merge_and_project(
            &mut state,
            &messages_update(vec![json!({"op": "set", "seq": 0, "msg": null})]),
        )
        .await
        .unwrap();
    assert_eq!(state["track.messages_chars"], json!(bytes(&msg_b)));
    assert_eq!(
        state["track.messages_chars"],
        json!(oracle_messages_chars(&state))
    );
    assert_eq!(state["messages"].as_array().unwrap().len(), 1);
}
/// 冷启动兜底：账本缺失且 messages 非空 → 首次 merge 全量重算（含存量消息）；
/// messages 恒空则保持键缺失。
#[tokio::test]
async fn messages_chars_cold_start_recompute() {
    let fixture = Fixture::build(&[]);
    let mut state = json!({
        // 恢复出的历史（自带 seq）——无 track.messages_chars
        "messages": [
            {"seq": 0, "role": "user", "content": "历史问题"},
            {"seq": 1, "role": "assistant", "content": "历史回答"},
        ],
    });
    let msg_new = json!({"role": "user", "content": "新消息"});
    fixture
        .executor
        .merge_and_project(
            &mut state,
            &messages_update(vec![json!({"op": "set", "msg": msg_new})]),
        )
        .await
        .unwrap();
    assert_eq!(
        state["track.messages_chars"],
        json!(oracle_messages_chars(&state)),
        "冷启动全量重算 = 存量 + 新增"
    );

    // messages 恒空（无 messages 键 + 空 ops）→ 键保持缺失
    let mut empty_state = json!({});
    fixture
        .executor
        .merge_and_project(&mut empty_state, &messages_update(vec![]))
        .await
        .unwrap();
    assert!(
        empty_state.get("track.messages_chars").is_none(),
        "空消息无账可记，保持键缺失"
    );
}
/// 性质：多批混合 ops（append/替换/删除/insert 顺延/同批同 seq 连写）累计记账
/// 与全量重算逐步一致（确定性伪随机序列，含中文与变长内容）。
#[tokio::test]
async fn messages_chars_multi_batch_accumulation_matches_recompute() {
    let fixture = Fixture::build(&[]);
    let mut state = json!({});
    let mut seed: u64 = 0x9E3779B97F4A7C15;
    let mut next_rand = move || {
        seed = seed
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        (seed >> 33) as usize
    };
    let mut msg_no = 0usize;
    let make_msg = |no: usize| json!({"role": "user", "content": format!("内容-{no}-中文-padding-{}", no * 37)});

    for round in 0..12usize {
        let seqs: Vec<i64> = state["messages"]
            .as_array()
            .map(|a| {
                a.iter()
                    .filter_map(|m| m.get("seq").and_then(|s| s.as_i64()))
                    .collect()
            })
            .unwrap_or_default();
        let mut ops: Vec<serde_json::Value> = Vec::new();
        for _ in 0..1 + next_rand() % 3 {
            let pick = next_rand() % 5;
            match pick {
                0 => {
                    // append
                    ops.push(json!({"op": "set", "msg": make_msg(msg_no)}));
                    msg_no += 1;
                }
                1 if !seqs.is_empty() => {
                    // 替换既有 seq（可能替换到本批刚写的消息——同批视角回放）
                    let seq = seqs[next_rand() % seqs.len()];
                    ops.push(json!({"op": "set", "seq": seq, "msg": make_msg(msg_no)}));
                    msg_no += 1;
                }
                2 if !seqs.is_empty() => {
                    // 删除既有 seq
                    let seq = seqs[next_rand() % seqs.len()];
                    ops.push(json!({"op": "set", "seq": seq, "msg": null}));
                }
                3 => {
                    // insert（后段 seq 顺延；字节口径剔除 seq 故内容字节不变）
                    let at = (next_rand() % (seqs.len() + 1)) as i64;
                    ops.push(json!({"op": "insert", "at": at, "msg": make_msg(msg_no)}));
                    msg_no += 1;
                }
                _ => {
                    // 同批同 seq 连写两次（顺序回放：后序 old 含前序效果）
                    if !seqs.is_empty() {
                        let seq = seqs[next_rand() % seqs.len()];
                        ops.push(json!({"op": "set", "seq": seq, "msg": make_msg(msg_no)}));
                        msg_no += 1;
                        ops.push(json!({"op": "set", "seq": seq, "msg": make_msg(msg_no)}));
                        msg_no += 1;
                    }
                }
            }
        }
        if ops.is_empty() {
            continue; // 该轮无可用 op（空数组起步的保护分支）
        }
        fixture
            .executor
            .merge_and_project(&mut state, &messages_update(ops))
            .await
            .unwrap();
        assert_eq!(
            state["track.messages_chars"],
            json!(oracle_messages_chars(&state)),
            "第 {round} 轮后累计记账 == 全量重算"
        );
    }
    // 性质收尾：最终数组非平凡（混合序列确实发生了增删改）
    assert!(state["messages"].as_array().unwrap().len() >= 3);
}
/// 全管道接线：插件经 run_compiled 回写 messages ops 后，最终 state 携带
/// track.messages_chars（invoke → merge_and_project 全链）。
#[tokio::test]
async fn messages_chars_booked_in_full_pipeline_run() {
    let fixture = Fixture::build(&["emitter"]);
    fixture.invoker.set_result(
        "emitter",
        PluginResult {
            state_updates: messages_update(vec![
                json!({"op": "set", "msg": {"role": "assistant", "content": "回复"}}),
            ]),
            ..Default::default()
        },
    );
    let final_state = fixture
        .run(
            &gated_body(vec![StepItem::Bare("emitter".into())]),
            &StepLibrary::default(),
            json!({}),
        )
        .await;
    assert_eq!(
        final_state["track.messages_chars"],
        json!(oracle_messages_chars(&final_state))
    );
}

/// 插件级失败常驻镜像 run 起点清零（装机 2026-11-18 634adfc6a364 实证根修）：
/// 上一 run 的 run.plugin_errors 残留（镜像「非空才写、从不清理」）会被
/// 挂起/长跑 run 的面板详情卡冒充成当前错误——run 起点必须两面清零
/// （常驻表 + 内存 state）。
#[tokio::test]
async fn run_start_clears_run_plugin_errors_mirror() {
    let sqlite = Arc::new(SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let tenant = TenantContext::new("tenant_mirror", "session_mirror");
    // 预置上一 run 的失败残留（stage_exec 终态非空才写的镜像形态）
    store
        .upsert_state_fields(
            "pipe-mirror",
            "tenant_mirror",
            &[(
                crate::RUN_PLUGIN_ERRORS_KEY.to_string(),
                serde_json::json!([{"plugin_id": "pipeline_llm_core", "code": "PARSE_ERROR", "message": "上一 run 的失败"}]),
            )]
            .into_iter()
            .collect(),
        )
        .await
        .unwrap();

    let invoker = Arc::new(MockInvoker::new());
    let executor = PipelineExecutor::new(
        invoker.clone() as Arc<dyn PluginInvoker>,
        PathBuf::from("."),
        tenant.clone(),
        ["emitter"].iter().map(|s| s.to_string()),
        store.clone(),
        "run_mirror_clear",
    );
    let compiled = compile_pipeline(
        &gated_body(vec![StepItem::Bare("emitter".into())]),
        &StepLibrary::default(),
        &executor.plugin_ids,
    )
    .expect("compile should succeed");
    executor
        .run_compiled(
            &compiled,
            json!({"pipeline_id": "pipe-mirror", "session_id": "thread_mirror"}),
        )
        .await
        .expect("run should succeed");

    let fields = store
        .load_pipeline_state("pipe-mirror", "tenant_mirror")
        .await
        .unwrap();
    assert_eq!(
        fields.get(crate::RUN_PLUGIN_ERRORS_KEY),
        Some(&serde_json::json!([])),
        "run 起点必须清零上一 run 的镜像残留"
    );
}
