// @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: rust-test
//! 核心运行循环与检查点（run_compiled / 步骤执行 / checkpoint / op ledger）。

use super::*;
use crate::pipeline_loop::journal::inject_run_message_id;
use crate::pipeline_loop::journal::op_ledger_entry;
#[tokio::test]
async fn invoke_plugin_labels_task_activity_at_wait_point() {
    let invoker = Arc::new(LabelCapturingInvoker {
        seen: Mutex::new(Vec::new()),
    });
    let store: Arc<dyn StorageBackend> = Arc::new(NullStorage::default());
    let executor = PipelineExecutor::new(
        invoker.clone() as Arc<dyn PluginInvoker>,
        PathBuf::from("."),
        TenantContext::new("tenant_test", "session_test"),
        std::iter::once("diag_step".to_string()).collect::<std::collections::HashSet<String>>(),
        store,
        "run_diag",
    );
    let config = PipelineConfig {
        name: "diag".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![PipelineStep {
                id: "body".into(),
                when: None,
                steps: vec![StepItem::Bare("diag_step".into())],
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
    let compiled = compile_pipeline(&config, &StepLibrary::default(), &executor.plugin_ids)
        .expect("compile should succeed");
    // 生产路径：run chain（api 层）经 scope 包住 run_compiled——本测试同构
    // 包一层，engine 深层的 set_current_label 经任务本地槽生效。
    let activity = agentos_core::task_activity::global_registry().register("diag-run");
    let _state = agentos_core::task_activity::scope(activity, async {
        executor
            .run_compiled(&compiled, json!({"pipeline_id": "p-diag"}))
            .await
            .expect("run should succeed")
    })
    .await;
    let seen = invoker.seen.lock().unwrap();
    assert_eq!(seen.len(), 1, "单 step 单调用");
    let (plugin_id, label) = &seen[0];
    assert_eq!(plugin_id, "diag_step");
    assert!(
        label.contains("invoking plugin diag_step") && label.contains("pipeline p-diag"),
        "等待点标签应含插件与管道（实际 {label}）"
    );
    // 无槽上下文（生产中非插桩任务路径）同函数不再 panic：current_label None。
    assert_eq!(agentos_core::task_activity::current_label(), None);
}
#[tokio::test]
async fn when_gate_false_skips_item_zero_calls() {
    let fixture = Fixture::build(&["a", "b"]);
    fixture.invoker.set_result(
        "a",
        PluginResult {
            state_updates: updates(&[("a_val", json!(1))]),
            ..Default::default()
        },
    );
    let config = gated_body(vec![
        StepItem::Bare("a".into()),
        StepItem::Gated {
            name: "b".into(),
            when: Some("False".into()),
            inputs: HashMap::new(),
        },
    ]);
    let final_state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(fixture.invoker.call_count("a"), 1);
    assert_eq!(
        fixture.invoker.call_count("b"),
        0,
        "when=False 的项必须零调用"
    );
    assert_eq!(final_state["a_val"], json!(1));
    assert!(final_state.get("b_val").is_none());
}
#[tokio::test]
async fn when_gate_true_runs_item() {
    let fixture = Fixture::build(&["a"]);
    let config = gated_body(vec![StepItem::Gated {
        name: "a".into(),
        when: Some("True".into()),
        inputs: HashMap::new(),
    }]);
    fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(fixture.invoker.call_count("a"), 1, "when=True 正常执行");
}
#[tokio::test]
async fn invalid_when_expression_fails_compilation() {
    // G10 语义升级：when 语法错误在加载期编译时暴露（不再静默 false 跳过）。
    // 兼容路径 run() 现场编译 → 返回 Err，调用方如实报错。
    let fixture = Fixture::build(&["a"]);
    let config = gated_body(vec![StepItem::Gated {
        name: "a".into(),
        when: Some("this is ((( invalid".into()),
        inputs: HashMap::new(),
    }]);
    let err = compile_pipeline(
        &config,
        &StepLibrary::default(),
        &fixture.executor.plugin_ids,
    )
    .expect_err("invalid when 应在编译期报错");
    assert!(err.to_string().contains("when"), "err: {err}");
}
#[tokio::test]
async fn group_when_false_skips_whole_step() {
    let fixture = Fixture::build(&["a", "b"]);
    let mut config = gated_body(vec![StepItem::Bare("a".into()), StepItem::Bare("b".into())]);
    config.loop_bodies[0].steps[0].when = Some("False".into());
    fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(
        fixture.invoker.call_count("a"),
        0,
        "组级 when=False 组内零调用"
    );
    assert_eq!(fixture.invoker.call_count("b"), 0);
}
#[tokio::test]
async fn gate_reads_state_updated_by_earlier_item() {
    // 门在到达时对 state 求值：前一项把 go 置 True 后，后一项的门放行
    let fixture = Fixture::build(&["a", "b"]);
    fixture.invoker.set_result(
        "a",
        PluginResult {
            state_updates: updates(&[("go", json!(true))]),
            ..Default::default()
        },
    );
    let config = gated_body(vec![
        StepItem::Bare("a".into()),
        StepItem::Gated {
            name: "b".into(),
            when: Some("go == True".into()),
            inputs: HashMap::new(),
        },
    ]);
    fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(fixture.invoker.call_count("a"), 1);
    assert_eq!(
        fixture.invoker.call_count("b"),
        1,
        "前项更新 state 后门应放行"
    );
}
#[tokio::test]
async fn step_item_inputs_reach_plugin_via_config_without_state_or_trace() {
    // step 项声明 inputs → 插件经 config["inputs"] 收到；不 merge 进 state、
    // 不产生 step diff（插件无其它 state_updates → 0 trace）。
    let fixture = Fixture::build(&["a"]);
    let config = gated_body(vec![StepItem::Gated {
        name: "a".into(),
        when: None,
        inputs: HashMap::from([("mode".into(), json!("strict")), ("limit".into(), json!(5))]),
    }]);
    let final_state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(
        fixture.invoker.captured_configs("a"),
        vec![json!({ "inputs": { "mode": "strict", "limit": 5 } })],
        "插件应经 config.inputs 收到 step 项声明的输入"
    );
    // 不污染 state：无 inputs / mode / limit 顶层键
    assert!(final_state.get("inputs").is_none());
    assert!(final_state.get("mode").is_none());
    assert!(final_state.get("limit").is_none());
    // 插件无产出 + inputs 不进 diff → 无 step 窗口轨迹（引擎窗口另计）
    assert!(
        fixture.store.step_trace_plugin_ids().is_empty(),
        "inputs 不得产生 step 轨迹"
    );
}
#[tokio::test]
async fn checkpoint_counts_configured_steps_group_skipped_excluded() {
    // 单次 body，4 个配置 step（s2 组级 when=False 跳过）；interval_steps=2 →
    // 按步：a(1) → c(2) 恰好触发一次 save(step_no=2)；s2 不计步。
    let fixture = Fixture::build(&["a", "b", "c", "d"]);
    let mut config = PipelineConfig {
        name: "ckpt".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![
                atomic_step("s1", "a"),
                atomic_step("s2", "b"),
                atomic_step("s3", "c"),
                atomic_step("s4", "d"),
            ],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        initial_state: HashMap::new(),
        max_rounds: None,
        checkpoint: CheckpointConfig {
            enabled: true,
            interval_steps: 2,
        },
    };
    // 组级 when=False：s2（→b）整个跳过，不计步
    config.loop_bodies[0].steps[1].when = Some("False".into());
    fixture
        .run(
            &config,
            &StepLibrary::default(),
            json!({ "pipeline_id": "p1" }),
        )
        .await;
    // 执行序列：a(1) → [s2 跳过] → c(2) → d(3)。interval=2 → 第 2 步触发一次
    // 中段存档（step_no=2）；run 结束时 persist_run_end 再落最终态（step_no=3）。
    assert_eq!(
            fixture.store.saved_step_nos(),
            vec![2, 3],
            "按步计：第 2 个实际执行的配置 step 触发中段存档；组级 when 跳过的 s2 不计；末尾为 run 结束兜底存档"
        );
    assert_eq!(fixture.store.save_count(), 2, "中段一次 + run 结尾兜底一次");
}
#[tokio::test]
async fn checkpoint_counts_steps_across_loop_rounds_not_rounds() {
    // 每轮 3 个配置 step，循环 2 轮 = 6 步；interval_steps=4 → 按步在第 4 个
    // step 触发一次存档（旧按轮：每 4 轮=12 步才触发）。counting invoker
    // 在第 6 次调用后 set ended 终止：r1 a/b/c + r2 a/b/c。
    let invoker = Arc::new(CountingInvoker {
        counter: Arc::new(AtomicUsize::new(0)),
        stop_after: 6,
        set_suspended_after: 0,
    });
    let store = Arc::new(NullStorage::default());
    let executor = PipelineExecutor::new(
        invoker.clone() as Arc<dyn PluginInvoker>,
        PathBuf::from("."),
        TenantContext::new("t", "s"),
        ["a", "b", "c"].iter().map(|s| s.to_string()),
        store.clone() as Arc<dyn StorageBackend>,
        "r",
    );
    let config = PipelineConfig {
        name: "ckpt_loop".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![
                atomic_step("s1", "a"),
                atomic_step("s2", "b"),
                atomic_step("s3", "c"),
            ],
            while_cond: Some("True".into()),
            exit_routes: vec![],
            run_on_error: false,
        }],
        initial_state: HashMap::new(),
        max_rounds: None,
        checkpoint: CheckpointConfig {
            enabled: true,
            interval_steps: 4,
        },
    };
    executor
        .run_compiled(
            &compile_pipeline(&config, &StepLibrary::default(), &executor.plugin_ids)
                .expect("compile should succeed"),
            json!({ "pipeline_id": "p1" }),
        )
        .await
        .expect("run ok");
    assert_eq!(
            store.saved_step_nos(),
            vec![4, 6],
            "按步计跨轮：第 4 个执行 step 触发中段存档（旧按轮则此时远未到阈值）；末尾 6 = run 结束兜底存档"
        );
}
#[tokio::test]
async fn when_gate_skipped_item_leaves_no_trace_and_no_call() {
    // a 有 state_updates 产出，但 when=False 跳过 → a 零调用；step 无 context
    // 变更 + 全部项被跳过 = 空 diff → persist_step_trace 早退不落 trace。
    let fixture = Fixture::build(&["a"]);
    fixture.invoker.set_result(
        "a",
        PluginResult {
            state_updates: updates(&[("a_val", json!(1))]),
            ..Default::default()
        },
    );
    let config = gated_body(vec![StepItem::Gated {
        name: "a".into(),
        when: Some("False".into()),
        inputs: HashMap::new(),
    }]);
    let final_state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(fixture.invoker.call_count("a"), 0, "when=False 零调用");
    assert!(final_state.get("a_val").is_none());
    assert!(
        fixture.store.step_trace_plugin_ids().is_empty(),
        "整 step 被 when 门架空 → 不落 step 轨迹"
    );
}
#[tokio::test]
async fn step_trace_patches_tool_result_keys_as_digest() {
    // 插件 state_updates 携带工具结果 staging 键 → 轨迹补丁里两键以摘要定位符
    // 落库（count/逐项 tool_name 保真），原文零残留；普通键原样镜像。
    let fixture = Fixture::build(&["a"]);
    let marker = "X".repeat(4096);
    fixture.invoker.set_result(
        "a",
        PluginResult {
            state_updates: updates(&[
                (
                    "_full_tool_results",
                    json!([
                        {"tool_name": "bash_execute", "data": {"stdout": marker}, "success": true},
                        {"tool_name": "file_read", "data": {"content": "第二项"}, "success": true},
                    ]),
                ),
                (
                    "tool_results",
                    json!([
                        {"tool_name": "bash_execute", "data": {"stdout": "截断展示版"}, "success": true},
                    ]),
                ),
                ("current_phase", json!("executing")),
            ]),
            ..Default::default()
        },
    );
    let config = PipelineConfig {
        name: "mirror".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![atomic_step("s1", "a")],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        initial_state: HashMap::new(),
        max_rounds: None,
        checkpoint: CheckpointConfig::default(),
    };
    fixture
        .run(
            &config,
            &StepLibrary::default(),
            json!({"pipeline_id": "p_mirror"}),
        )
        .await;

    let traces = fixture.store.traces.lock().unwrap();
    let step_patch = traces
        .iter()
        .find(|t| t.plugin_id == "s1")
        .map(|t| &t.patch_data)
        .expect("step 窗口应有轨迹行（插件产出了 state_updates）");
    let patch_str = step_patch.to_string();
    assert!(!patch_str.contains("XXXX"), "全文零残留: {patch_str}");
    assert!(!patch_str.contains("截断展示版"), "展示版原文零残留");

    let full_digest = &step_patch["_full_tool_results"];
    assert_eq!(full_digest["_trace_digest"], json!(true), "摘要标记在场");
    assert_eq!(full_digest["count"], json!(2), "条数保真");
    let names: Vec<&str> = full_digest["items"]
        .as_array()
        .expect("items 数组在场")
        .iter()
        .map(|i| i["tool_name"].as_str().unwrap_or(""))
        .collect();
    assert_eq!(names, vec!["bash_execute", "file_read"], "逐项 tool_name");
    assert!(
        full_digest["items"][0]["chars"].as_u64().unwrap_or(0) > 4096,
        "chars 反映原条目体量"
    );

    let display_digest = &step_patch["tool_results"];
    assert_eq!(display_digest["_trace_digest"], json!(true));
    assert_eq!(display_digest["count"], json!(1));

    assert_eq!(
        step_patch["current_phase"],
        json!("executing"),
        "普通键不受定位符化影响，原样镜像"
    );
}
#[test]
fn trace_digest_passthrough_non_array() {
    let v = json!({"not": "an array"});
    assert_eq!(
        trace_mirror_digest(&v),
        v,
        "非数组输入原样透传（形状契约外的值不做有损变换）"
    );
}
#[test]
fn trace_digest_empty_array_zero_count() {
    let d = trace_mirror_digest(&json!([]));
    assert_eq!(d["_trace_digest"], json!(true));
    assert_eq!(d["count"], json!(0));
    assert_eq!(d["items"], json!([]));
}
#[test]
fn trace_digest_far_smaller_leak_free_and_deterministic() {
    let original = json!([
        {"tool_name": "bash_execute", "data": {"stdout": "X".repeat(4096)}, "success": true},
        {"tool_name": "file_read", "data": {"content": "y".repeat(2048)}, "success": true},
    ]);
    let digest = trace_mirror_digest(&original);
    let orig_len = original.to_string().len();
    let digest_len = digest.to_string().len();
    assert!(
        digest_len * 10 < orig_len,
        "摘要应远小于原文（>10x）：digest={digest_len}B original={orig_len}B"
    );
    assert!(!digest.to_string().contains("XXX"), "摘要零内容泄漏");
    assert_eq!(
        digest,
        trace_mirror_digest(&original),
        "同输入摘要确定（幂等锚）"
    );
}
#[tokio::test]
async fn test_single_step_atomic() {
    // 一个 step 引用原子插件；验证插件被调用 + state_updates 被 merge
    let fixture = Fixture::build(&["echo"]);
    fixture.invoker.set_result(
        "echo",
        PluginResult {
            state_updates: updates(&[("reply", json!("hello"))]),
            ..Default::default()
        },
    );
    let config = PipelineConfig {
        name: "single".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),

            steps: vec![atomic_step("s1", "echo")],

            while_cond: None,
            exit_routes: vec![],

            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let library = StepLibrary::default();
    let final_state = fixture.run(&config, &library, json!({})).await;
    assert_eq!(fixture.invoker.call_count("echo"), 1);
    assert_eq!(final_state["reply"], json!("hello"));
    // ended 默认 false
    assert_eq!(final_state["ended"], json!(false));
}
#[tokio::test]
async fn test_composite_step() {
    // 一个组合 step 引用两个子 step（命中规则①：当前管道 step id），验证递归执行。
    //
    // 注意 spec 语义：execute_steps 会遍历 config.steps 中【所有】step，
    // 而 find_step 也在 config.steps 中查。如果 child_a/child_b 同时在 config.steps 里，
    // 它们会被顶层 execute_steps 跑一次，又被 parent 的递归引用跑一次（共 2 次）。
    // 为了精确验证"递归执行"（而不是重复触发），这里只把 parent 放进 config.steps，
    // 子 step 放进公共 step 库——它们只能通过 parent 的引用被命中（命中规则②）。
    let fixture = Fixture::build(&["a", "b"]);
    fixture.invoker.set_result(
        "a",
        PluginResult {
            state_updates: updates(&[("a_val", json!(1))]),
            ..Default::default()
        },
    );
    fixture.invoker.set_result(
        "b",
        PluginResult {
            state_updates: updates(&[("b_val", json!(2))]),
            ..Default::default()
        },
    );
    let config = PipelineConfig {
        name: "composite".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),

            steps: vec![PipelineStep {
                id: "parent".into(),
                steps: vec!["child_a".into(), "child_b".into()],
                when: None,
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
    let mut library = StepLibrary::default();
    library
        .steps
        .insert("child_a".to_string(), atomic_step("child_a", "a"));
    library
        .steps
        .insert("child_b".to_string(), atomic_step("child_b", "b"));
    let final_state = fixture.run(&config, &library, json!({})).await;
    assert_eq!(fixture.invoker.call_count("a"), 1);
    assert_eq!(fixture.invoker.call_count("b"), 1);
    assert_eq!(final_state["a_val"], json!(1));
    assert_eq!(final_state["b_val"], json!(2));
}
#[tokio::test]
async fn test_composite_step_double_trigger_semantics() {
    // 补充验证 spec 语义：如果子 step 同时在 config.steps 里，会被执行 2 次
    // （顶层 execute_steps 一次 + parent 递归一次）。这验证"重复触发"是 spec 的
    // 既定行为，而非 bug。
    let fixture = Fixture::build(&["a"]);
    fixture.invoker.set_result(
        "a",
        PluginResult {
            state_updates: updates(&[("a_val", json!(1))]),
            ..Default::default()
        },
    );
    let config = PipelineConfig {
        name: "double".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),

            steps: vec![
                PipelineStep {
                    id: "parent".into(),
                    steps: vec!["child".into()],
                    when: None,
                    context: HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                },
                atomic_step("child", "a"),
            ],

            while_cond: None,
            exit_routes: vec![],

            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let _ = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    // 顶层执行 parent（递归触发 child 1 次）+ 顶层执行 child（1 次）= 2 次
    assert_eq!(fixture.invoker.call_count("a"), 2);
}
#[tokio::test]
async fn test_step_library() {
    // step 引用公共 step 库的 id（命中规则②）
    let fixture = Fixture::build(&["lib_plugin"]);
    fixture.invoker.set_result(
        "lib_plugin",
        PluginResult {
            state_updates: updates(&[("lib_out", json!("from_library"))]),
            ..Default::default()
        },
    );
    let config = PipelineConfig {
        name: "with_lib".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),

            steps: vec![atomic_step("caller", "shared_step")],

            while_cond: None,
            exit_routes: vec![],

            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let mut library = StepLibrary::default();
    library.steps.insert(
        "shared_step".to_string(),
        atomic_step("shared_step", "lib_plugin"),
    );
    let final_state = fixture.run(&config, &library, json!({})).await;
    assert_eq!(fixture.invoker.call_count("lib_plugin"), 1);
    assert_eq!(final_state["lib_out"], json!("from_library"));
}
#[tokio::test]
async fn test_loop() {
    // loop.enabled=true，插件每次递增计数器；第 3 次 set ended=true 后停止
    let counter = Arc::new(AtomicUsize::new(0));
    let executor = make_executor(
        Arc::new(CountingInvoker {
            counter: counter.clone(),
            stop_after: 3,
            set_suspended_after: 0, // 不挂起
        }) as Arc<dyn PluginInvoker>,
        &["counter_plugin"],
    );
    let config = PipelineConfig {
        name: "loop_test".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![atomic_step("body", "counter_plugin")],
            while_cond: Some("True".into()), // 恒真循环，靠 ended 退出
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
                .expect("compile should succeed"),
            json!({}),
        )
        .await
        .unwrap();
    // 验证循环到第 3 次因 ended 停止
    assert_eq!(counter.load(Ordering::SeqCst), 3);
    assert_eq!(state["round"], json!(3));
    assert_eq!(state["ended"], json!(true));
}
#[tokio::test]
async fn test_routes() {
    // routes when 条件匹配后 set 字段 + ended
    let fixture = Fixture::build(&[]);
    let config = PipelineConfig::single_body(
        "routes",
        None,
        vec![PipelineStep {
            id: "router".into(),
            steps: vec![], // 不调任何插件
            when: None,
            context: HashMap::new(),
            routes: vec![Route {
                when: "core_type == 'tool_execute'".into(),
                then: agentos_core::types::RouteAction {
                    next: RouteNext::End,
                    set: updates(&[("handled", json!(true))]),
                },
            }],
            loop_config: None,
        }],
    );
    let library = StepLibrary::default();
    let state = fixture
        .run(&config, &library, json!({ "core_type": "tool_execute" }))
        .await;
    assert_eq!(state["handled"], json!(true));
    assert_eq!(state["ended"], json!(true));
}
#[tokio::test]
async fn test_routes_wait() {
    // Wait → suspended=true
    let fixture = Fixture::build(&[]);
    let config = PipelineConfig {
        name: "wait".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),

            steps: vec![PipelineStep {
                id: "w".into(),
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

            while_cond: None,
            exit_routes: vec![],

            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(state["suspended"], json!(true));
}
#[tokio::test]
async fn test_unknown_reference_fails_compilation() {
    // G10 语义升级：引用不存在的 step/插件在加载期编译时报错（不静默 error log）。
    let fixture = Fixture::build(&[]);
    let config = PipelineConfig {
        name: "miss".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),

            steps: vec![
                atomic_step("s1", "ghost_step"), // 既不是 step 也不是已知插件
                atomic_step("s2", "ghost_plugin"),
            ],

            while_cond: None,
            exit_routes: vec![],

            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let err = compile_pipeline(
        &config,
        &StepLibrary::default(),
        &fixture.executor.plugin_ids,
    )
    .expect_err("未知引用应在编译期报错");
    assert!(err.to_string().contains("ghost_step"), "err: {err}");
}
#[tokio::test]
async fn test_context_injection() {
    // step.context 的 {{state.xxx}} 被渲染注入到 state
    let fixture = Fixture::build(&["reader"]);
    // 插件读 ctx.state 里的 injected 字段，写回 state 确认注入生效
    fixture.invoker.set_result(
        "reader",
        PluginResult {
            state_updates: updates(&[("seen", json!("{{state.injected}}"))]),
            ..Default::default()
        },
    );
    // 注意：上面结果也会被渲染，但 state_updates merge 时不渲染。
    // 我们改为：context 注入后，直接用 state.injected 验证（不依赖插件）。
    // 这里再写一个 context 注入测试。
    let _ = fixture;
    let fixture2 = Fixture::build(&[]);
    let mut context = HashMap::new();
    context.insert("injected".to_string(), json!("agent={{state.agent_id}}"));
    let config = PipelineConfig::single_body(
        "ctx",
        None,
        vec![PipelineStep {
            id: "ctx_step".into(),
            steps: vec![],
            when: None,
            context,
            routes: vec![Route {
                when: "injected == 'agent=A1'".into(),
                then: agentos_core::types::RouteAction {
                    next: RouteNext::End,
                    set: updates(&[("matched", json!(true))]),
                },
            }],
            loop_config: None,
        }],
    );
    let state = fixture2
        .run(
            &config,
            &StepLibrary::default(),
            json!({ "agent_id": "A1" }),
        )
        .await;
    // injected 模板被渲染为 "agent=A1"
    assert_eq!(state["injected"], json!("agent=A1"));
    // 路由条件命中（证明注入确实发生在路由求值之前）
    assert_eq!(state["matched"], json!(true));
    assert_eq!(state["ended"], json!(true));
}
#[tokio::test]
async fn test_skip_remaining() {
    // 插件返回 skip_remaining=true → 跳过同一 step.steps 中后续项
    let fixture = Fixture::build(&["first", "second"]);
    fixture.invoker.set_result(
        "first",
        PluginResult {
            state_updates: updates(&[("first_done", json!(true))]),
            skip_remaining: true,
            ..Default::default()
        },
    );
    fixture.invoker.set_result(
        "second",
        PluginResult {
            state_updates: updates(&[("second_done", json!(true))]),
            ..Default::default()
        },
    );
    let config = PipelineConfig {
        name: "skip".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),

            steps: vec![PipelineStep {
                id: "both".into(),
                steps: vec!["first".into(), "second".into()],
                when: None,
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
    let state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(state["first_done"], json!(true));
    // second 不应被调用
    assert_eq!(fixture.invoker.call_count("second"), 0);
    assert!(state.get("second_done").is_none());
}
#[tokio::test]
async fn test_plugin_error_continues() {
    // 插件返回 error → warn + 继续后续 step（不 panic）
    let fixture = Fixture::build(&["bad", "good"]);
    fixture.invoker.set_result(
        "bad",
        PluginResult {
            error: Some(PluginError {
                message: "boom".into(),
                code: None,
                source: None,
            }),
            ..Default::default()
        },
    );
    fixture.invoker.set_result(
        "good",
        PluginResult {
            state_updates: updates(&[("ok", json!(true))]),
            ..Default::default()
        },
    );
    let config = PipelineConfig {
        name: "err".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),

            steps: vec![PipelineStep {
                id: "seq".into(),
                steps: vec!["bad".into(), "good".into()],
                when: None,
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
    let state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    // good 仍被调用
    assert_eq!(fixture.invoker.call_count("good"), 1);
    assert_eq!(state["ok"], json!(true));
    // 插件错误收集到 _plugin_errors（引擎内部键，api 层提取为 plugin_errors）
    let errs = state["_plugin_errors"]
        .as_array()
        .expect("_plugin_errors 应为数组");
    assert_eq!(errs.len(), 1);
    assert_eq!(errs[0]["plugin_id"], json!("bad"));
    assert_eq!(errs[0]["code"], json!("PLUGIN_EXEC_FAILED"));
    assert_eq!(errs[0]["message"], json!("boom"));
}
#[tokio::test]
async fn test_plugin_error_continues_with_code() {
    // 插件返回带 code 的 error → 收集保留 code（前端通知按 code 渲染）
    let fixture = Fixture::build(&["bad"]);
    fixture.invoker.set_result(
        "bad",
        PluginResult {
            error: Some(PluginError {
                message: "sidecar crashed".into(),
                code: Some("PLUGIN_CRASHED".into()),
                source: Some("plugin".into()),
            }),
            ..Default::default()
        },
    );
    let config = PipelineConfig {
        name: "err_code".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![atomic_step("s1", "bad")],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    let errs = state["_plugin_errors"]
        .as_array()
        .expect("_plugin_errors 应为数组");
    assert_eq!(errs.len(), 1);
    assert_eq!(errs[0]["plugin_id"], json!("bad"));
    assert_eq!(errs[0]["code"], json!("PLUGIN_CRASHED"));
    assert_eq!(errs[0]["message"], json!("sidecar crashed"));
}
#[tokio::test]
async fn test_invoker_error_collected() {
    // invoker 自身报错（如 sidecar 不可达）→ 同样收集到 _plugin_errors
    let fixture = Fixture::build(&["bad"]);
    fixture.invoker.set_err(
        "bad",
        PluginError {
            message: "sidecar unreachable".into(),
            code: Some("MCP_CALL_FAILED".into()),
            source: None,
        },
    );
    let config = PipelineConfig {
        name: "inv_err".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![atomic_step("s1", "bad")],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    let errs = state["_plugin_errors"]
        .as_array()
        .expect("_plugin_errors 应为数组");
    assert_eq!(errs.len(), 1);
    assert_eq!(errs[0]["plugin_id"], json!("bad"));
    assert_eq!(errs[0]["code"], json!("MCP_CALL_FAILED"));
    assert_eq!(errs[0]["message"], json!("sidecar unreachable"));
}
#[test]
fn test_plugin_error_ledger_aggregates_and_caps() {
    // 有界账本（用户裁定 2026-11-17）：同文重复只累加 count；不同文本各留
    // 一条；条目数到上限后新文本直接丢弃——state 不再随失败次数线性膨胀。
    use crate::pipeline_loop::finalize::{
        materialize_plugin_errors, record_plugin_error, MAX_PLUGIN_ERROR_ENTRIES,
    };
    let err = |msg: &str| PluginError {
        message: msg.into(),
        code: Some("X".into()),
        source: None,
    };
    let mut state = json!({});
    for _ in 0..5 {
        record_plugin_error(&mut state, "p", &err("same"));
    }
    for i in 0..(MAX_PLUGIN_ERROR_ENTRIES + 4) {
        record_plugin_error(&mut state, "p", &err(&format!("distinct-{i}")));
    }

    let stats = state["_plugin_error_stats"]
        .as_array()
        .expect("账本应为数组");
    assert_eq!(
        stats.len(),
        MAX_PLUGIN_ERROR_ENTRIES,
        "账本条目数有界（超出的新文本丢弃）"
    );
    assert_eq!(stats[0]["message"], json!("same"));
    assert_eq!(stats[0]["count"], json!(5), "同文重复只累加计数");
    assert!(
        stats.iter().all(|e| e["message"] != json!("distinct-7")),
        "超上限的新文本不得进入账本: {stats:?}"
    );

    materialize_plugin_errors(&mut state);
    let errs = state["_plugin_errors"]
        .as_array()
        .expect("物化后的最终结论数组");
    assert_eq!(errs.len(), MAX_PLUGIN_ERROR_ENTRIES);
    assert_eq!(errs[0]["count"], json!(5), "物化保留计数");
    assert!(
        errs.iter().all(|e| e.get("seq").is_none()),
        "seq 是账本内部字段，不对外暴露"
    );
}
#[test]
fn test_plugin_error_message_truncated_to_bound() {
    // 单条消息截断上界：长异常消息不得把 state 撑到 MB 级。
    use crate::pipeline_loop::finalize::{record_plugin_error, MAX_PLUGIN_ERROR_MESSAGE_CHARS};
    let mut state = json!({});
    let long = "错".repeat(MAX_PLUGIN_ERROR_MESSAGE_CHARS + 50);
    record_plugin_error(
        &mut state,
        "p",
        &PluginError {
            message: long,
            code: None,
            source: None,
        },
    );
    let msg = state["_plugin_error_stats"][0]["message"]
        .as_str()
        .expect("message 字符串");
    assert_eq!(
        msg.chars().count(),
        MAX_PLUGIN_ERROR_MESSAGE_CHARS + 1,
        "截断到上界 + 省略号"
    );
    assert!(msg.ends_with('…'), "截断须留省略号标记");
}
#[test]
fn test_merge_plugin_error_stats_sums_without_reappending() {
    // 并行迭代归并：父账本按 (plugin_id, message) 累加迭代计数，不再追加
    // 明细数组（历史指数膨胀根因 = 迭代克隆带入父账本后重复 append）。
    use crate::pipeline_loop::finalize::{
        merge_plugin_error_stats, record_plugin_error, MAX_PLUGIN_ERROR_ENTRIES,
    };
    let err = |msg: &str| PluginError {
        message: msg.into(),
        code: Some("X".into()),
        source: None,
    };
    let mut parent = json!({});
    record_plugin_error(&mut parent, "p", &err("same"));
    record_plugin_error(&mut parent, "p", &err("same"));

    let mut iter_state = json!({});
    for _ in 0..3 {
        record_plugin_error(&mut iter_state, "p", &err("same"));
    }
    merge_plugin_error_stats(&mut parent, &iter_state);

    let stats = parent["_plugin_error_stats"].as_array().unwrap();
    assert_eq!(stats.len(), 1, "同文归并后仍是一条");
    assert_eq!(stats[0]["count"], json!(5), "父 2 + 迭代 3 累加");

    let mut full = json!({});
    for i in 0..MAX_PLUGIN_ERROR_ENTRIES {
        record_plugin_error(&mut full, "p", &err(&format!("parent-{i}")));
    }
    let mut extra = json!({});
    record_plugin_error(&mut extra, "p", &err("overflow"));
    merge_plugin_error_stats(&mut full, &extra);
    assert_eq!(
        full["_plugin_error_stats"].as_array().unwrap().len(),
        MAX_PLUGIN_ERROR_ENTRIES,
        "归并不突破条目上限"
    );
}
#[tokio::test]
async fn test_suspended_stops_loop() {
    // 管道级 loop，插件 set suspended=true → 循环停止（不进入下一轮）
    let counter = Arc::new(AtomicUsize::new(0));
    let executor = make_executor(
        Arc::new(CountingInvoker {
            counter: counter.clone(),
            stop_after: 0,
            set_suspended_after: 2,
        }) as Arc<dyn PluginInvoker>,
        &["p"],
    );
    let config = PipelineConfig {
        name: "suspend_loop".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![atomic_step("body", "p")],
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
                .expect("compile should succeed"),
            json!({}),
        )
        .await
        .unwrap();
    // 跑了 2 轮就停
    assert_eq!(counter.load(Ordering::SeqCst), 2);
    assert_eq!(state["suspended"], json!(true));
    // ended 未被设置
    assert_eq!(state["ended"], json!(false));
}
#[tokio::test]
async fn test_dynamic_plugin_name() {
    // step.steps 里含 {{state.core_plugin}} 模板 → 渲染后命中插件
    let fixture = Fixture::build(&["real_core"]);
    fixture.invoker.set_result(
        "real_core",
        PluginResult {
            state_updates: updates(&[("executed", json!(true))]),
            ..Default::default()
        },
    );
    let config = PipelineConfig {
        name: "dyn".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),

            steps: vec![PipelineStep {
                id: "dyn_step".into(),
                steps: vec!["{{state.core_plugin}}".into()],
                when: None,
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
    let state = fixture
        .run(
            &config,
            &StepLibrary::default(),
            json!({ "core_plugin": "real_core" }),
        )
        .await;
    assert_eq!(fixture.invoker.call_count("real_core"), 1);
    assert_eq!(state["executed"], json!(true));
}
#[tokio::test]
async fn test_non_object_initial_state_becomes_object() {
    // 非对象 initial_state 应被规范化为空对象，不 panic
    let fixture = Fixture::build(&[]);
    let config = PipelineConfig {
        name: "noop".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),

            steps: vec![],

            while_cond: None,
            exit_routes: vec![],

            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let state = fixture
        .run(&config, &StepLibrary::default(), serde_json::Value::Null)
        .await;
    assert!(state.is_object());
    assert_eq!(state["ended"], json!(false));
}
#[tokio::test]
async fn test_route_step_jumps_to_target_step() {
    // G10 语义升级：RouteNext::Step(id) 真跳转——j 命中后跳到 t 执行，
    // 不再写 state.next_step 记号。跳转目标须在本循环体（编译期校验）。
    let fixture = Fixture::build(&["a"]);
    let config = PipelineConfig {
        name: "jump".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),

            steps: vec![
                PipelineStep {
                    id: "j".into(),
                    steps: vec![],
                    when: None,
                    context: HashMap::new(),
                    routes: vec![Route {
                        when: "True".into(),
                        then: agentos_core::types::RouteAction {
                            next: RouteNext::Step("t".into()),
                            set: HashMap::new(),
                        },
                    }],
                    loop_config: None,
                },
                PipelineStep {
                    id: "t".into(),
                    steps: vec!["a".into()],
                    when: None,
                    context: HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                },
            ],

            while_cond: None,
            exit_routes: vec![],

            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(
        fixture.invoker.call_count("a"),
        1,
        "j 跳转到 t 后 t 的插件被执行"
    );
    assert!(
        state.get("next_step").is_none(),
        "Step 真跳转不再写 next_step 记号"
    );
}
#[tokio::test]
async fn test_step_route_self_jump_guard_errors() {
    // 恒跳回自身的 step 路由 → 跳转护栏截断为 Err（不无限执行）。
    // 上限 = steps.len()×4（本例 1 步 → max(4,16)=16 次跳转）：
    // 第 17 次跳转超限返回 Err，插件恰好被调用 17 次（每次跳转前执行一次）。
    let counter = Arc::new(AtomicUsize::new(0));
    let executor = make_executor(
        Arc::new(CountingInvoker {
            counter: counter.clone(),
            stop_after: 0,
            set_suspended_after: 0,
        }) as Arc<dyn PluginInvoker>,
        &["p"],
    );
    let mut jumping = atomic_step("looper", "p");
    jumping.routes = vec![Route {
        when: "True".into(),
        then: agentos_core::types::RouteAction {
            next: RouteNext::Step("looper".into()),
            set: HashMap::new(),
        },
    }];
    let config = PipelineConfig {
        name: "self_jump".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![jumping],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let result = executor
        .run_compiled(
            &compile_pipeline(&config, &StepLibrary::default(), &executor.plugin_ids)
                .expect("compile should succeed"),
            json!({}),
        )
        .await;
    let err = result.expect_err("恒自跳路由应被护栏截断为 Err");
    let msg = err.to_string();
    assert!(
        msg.contains("step 级跳转次数超限") && msg.contains("死循环"),
        "错误信息应说明疑似 step 路由死循环：{msg}"
    );
    assert_eq!(
        counter.load(Ordering::SeqCst),
        17,
        "1 步管道上限 16 次跳转，第 17 次截断（每次跳转前插件执行一次）"
    );
}
#[tokio::test]
async fn test_step_level_loop() {
    // step 自带 loop_config（非管道级）：循环执行 step.steps
    let counter = Arc::new(AtomicUsize::new(0));
    let executor = make_executor(
        Arc::new(CountingInvoker {
            counter: counter.clone(),
            stop_after: 3,
            set_suspended_after: 0,
        }) as Arc<dyn PluginInvoker>,
        &["p"],
    );
    let config = PipelineConfig {
        name: "step_loop".into(),
        loop_bodies: vec![LoopBody {
            // 管道级不循环
            id: "main".into(),
            steps: vec![PipelineStep {
                id: "looper".into(),
                steps: vec!["p".into()],
                when: None,
                context: HashMap::new(),
                routes: vec![],
                loop_config: Some(agentos_core::types::LoopConfig {
                    enabled: true,
                    max_iterations: -1,
                    ..Default::default()
                }),
            }],
            while_cond: None,
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
                .expect("compile should succeed"),
            json!({}),
        )
        .await
        .unwrap();
    // step 自带循环跑了 3 轮（第 3 次 set ended）
    assert_eq!(counter.load(Ordering::SeqCst), 3);
    assert_eq!(state["ended"], json!(true));
}
#[tokio::test]
async fn test_loop_does_not_inject_agent_config_per_iteration() {
    // 契约（双轨收敛）：agent 全量配置唯一事实源 = agent_config_load 插件；
    // 内核 engine 不再每轮迭代把 agent yaml 注入 state（即使存在
    // config/agents/<id>.yaml），也不打 _agent_config_missing 标记。
    let temp = tempfile::tempdir().unwrap();
    let agents_dir = temp.path().join("agents");
    std::fs::create_dir_all(&agents_dir).unwrap();
    std::fs::write(
        agents_dir.join("reload_test.yaml"),
        "system_prompt: 注入的提示词\ncustom: hello\n",
    )
    .unwrap();

    let fixture = Fixture::build(&["noop"]);
    fixture.invoker.set_result(
        "noop",
        PluginResult {
            state_updates: updates(&[("ended", json!(true))]),
            ..Default::default()
        },
    );

    let executor = make_executor(fixture.invoker.clone(), &["noop"]);

    let config = PipelineConfig {
        name: "reload_test".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![atomic_step("s1", "noop")],
            while_cond: Some("True".into()),
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    // initial_state 只有 agent_id，没有 system_prompt/custom
    let initial = json!({"agent_id": "reload_test"});

    let final_state = executor
        .run_compiled(
            &compile_pipeline(&config, &StepLibrary::default(), &executor.plugin_ids)
                .expect("compile should succeed"),
            initial,
        )
        .await
        .unwrap();

    // loop 已跑过（ended 由插件置位），agent yaml 仍不得注入 state
    assert_eq!(final_state["ended"], json!(true), "loop 应正常执行");
    assert!(
        final_state.get("system_prompt").is_none(),
        "agent 全量配置不得由 engine 注入（agent_config_load 是唯一事实源）"
    );
    assert!(
        final_state.get("_agent_config_missing").is_none(),
        "engine 不读 agent yaml，也不得打配置缺失标记"
    );
}
#[test]
fn test_inject_run_message_id_no_message_id_untouched() {
    // state 无 message_id（或为空）→ ops 原样返回，标志不置位。
    let mut state = json!({"messages": []});
    let ops = vec![json!({"op": "set", "msg": {"role": "assistant", "content": "x"}})];
    let out = inject_run_message_id(&mut state, &ops, -1);
    assert_eq!(out, ops, "无 message_id 时 op 应原样返回");
    assert!(state.get("_assistant_id_assigned").is_none());
}
#[test]
fn test_inject_run_message_id_flag_already_set_untouched() {
    // _assistant_id_assigned=true → 不再注入（每 run 仅一次）。
    let mut state = json!({"message_id": "m_1", "_assistant_id_assigned": true});
    let ops = vec![json!({"op": "set", "msg": {"role": "assistant", "content": "x"}})];
    let out = inject_run_message_id(&mut state, &ops, -1);
    assert_eq!(out, ops);
}
#[test]
fn test_inject_run_message_id_skips_insert_ops() {
    // 非 set 的 op（insert）跳过。
    let mut state = json!({"message_id": "m_2"});
    let ops = vec![json!({"op": "insert", "at": 0, "msg": {"role": "assistant", "content": "x"}})];
    let out = inject_run_message_id(&mut state, &ops, -1);
    assert_eq!(out, ops);
    assert!(state.get("_assistant_id_assigned").is_none());
}
#[test]
fn test_inject_run_message_id_skips_old_slot_and_non_object_msg() {
    // 旧槽位（seq <= entry_max_seq）与 msg 非对象 → 均跳过。
    let mut state = json!({"message_id": "m_3"});
    let ops = vec![
        json!({"op": "set", "seq": 0, "msg": {"role": "assistant", "content": "old"}}),
        json!({"op": "set", "seq": 1, "msg": "not-an-object"}),
        json!({"op": "set", "seq": 2, "msg": null}),
    ];
    let out = inject_run_message_id(&mut state, &ops, 5);
    assert_eq!(out, ops);
    assert!(state.get("_assistant_id_assigned").is_none());
}
#[test]
fn test_inject_run_message_id_skips_msg_with_own_id() {
    // assistant 消息自带 id → 不注入（有权威 id 的不用占位）。
    let mut state = json!({"message_id": "m_4"});
    let ops = vec![json!({
        "op": "set",
        "seq": 7,
        "msg": {"role": "assistant", "content": "x", "id": "own_123"}
    })];
    let out = inject_run_message_id(&mut state, &ops, 1);
    assert_eq!(out, ops);
    assert!(state.get("_assistant_id_assigned").is_none());
}
#[test]
fn test_inject_run_message_id_only_first_assistant_append() {
    // 命中：首个新槽位 assistant 追加 op 挂 _message_id + 置位；同批后续不再注入。
    // 注：注入前引擎已把无 seq 的 set 解析为递增 seq（见 apply_messages_op_update），
    // 故此处传带 seq 的 resolved op。
    let mut state = json!({"message_id": "a_run_9"});
    let ops = vec![
        json!({"op": "set", "seq": 1, "msg": {"role": "user", "content": "q"}}),
        json!({"op": "set", "seq": 2, "msg": {"role": "assistant", "content": "第一轮"}}),
        json!({"op": "set", "seq": 3, "msg": {"role": "assistant", "content": "第二轮"}}),
    ];
    let out = inject_run_message_id(&mut state, &ops, 0);
    assert_eq!(out[0].get("_message_id"), None, "user 不注入");
    assert_eq!(out[1]["_message_id"], "a_run_9", "首个 assistant 注入");
    assert_eq!(out[2].get("_message_id"), None, "同批后续不注入");
    assert_eq!(state["_assistant_id_assigned"], true);
    // 注入只挂 op 上，不改消息体
    assert!(out[1]["msg"].get("id").is_none());
}
#[test]
fn test_apply_slot_ops_insert_shifts_subsequent_seqs() {
    let mut arr = vec![
        json!({"seq": 0, "role": "user"}),
        json!({"seq": 1, "role": "assistant"}),
    ];
    apply_slot_ops_to_array(
        &mut arr,
        &[json!({
            "op": "insert", "at": 0, "msg": {"role": "system"}
        })],
    );
    let seqs: Vec<i64> = arr.iter().map(|m| m["seq"].as_i64().unwrap()).collect();
    assert_eq!(seqs, vec![0, 1, 2], "insert at 0 后段顺延");
    assert_eq!(arr[0]["role"], "system");
}
#[test]
fn test_apply_slot_ops_delete_missing_seq_noop() {
    let mut arr = vec![json!({"seq": 0, "role": "user"})];
    apply_slot_ops_to_array(&mut arr, &[json!({"op": "set", "seq": 5, "msg": null})]);
    assert_eq!(arr.len(), 1, "删除不存在的 seq 应 no-op");
}
#[test]
fn test_apply_slot_ops_ignores_unknown_and_seqless_ops() {
    let mut arr = vec![json!({"seq": 0, "role": "user"})];
    apply_slot_ops_to_array(
        &mut arr,
        &[
            json!({"op": "mystery", "seq": 3, "msg": {"role": "x"}}),
            json!({"op": "set", "msg": {"role": "y"}}),
            json!({"op": "insert", "msg": {"role": "z"}}),
        ],
    );
    assert_eq!(arr.len(), 1, "未知 op / 缺 seq / 缺 at 全部忽略");
}
#[test]
fn test_apply_slot_ops_modify_replaces_same_seq() {
    let mut arr = vec![json!({"seq": 0, "role": "user", "content": "old"})];
    apply_slot_ops_to_array(
        &mut arr,
        &[json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "new"}})],
    );
    assert_eq!(arr.len(), 1);
    assert_eq!(arr[0]["content"], "new");
    assert_eq!(arr[0]["seq"], 0, "modify 保留原 seq");
}
#[test]
fn test_op_ledger_entry_insert_and_unknown() {
    // insert 实录：{op, at, message_id, blob_id}
    let insert = op_ledger_entry(&json!({
        "op": "insert", "at": 2, "msg": {"role": "assistant", "content": "hi"}
    }));
    let ins = insert.expect("insert 应产生实录");
    assert_eq!(ins["op"], "insert");
    assert_eq!(ins["at"], 2);
    assert!(ins["message_id"].as_str().unwrap().starts_with("mc_"));
    assert!(ins["blob_id"].is_string());

    // 未知 op → 跳过（前向兼容）
    assert!(op_ledger_entry(&json!({"op": "mystery", "seq": 1})).is_none());
}
#[test]
fn test_op_ledger_entry_set_without_msg_ids_null() {
    // set 但 msg 缺失（delete）→ message_id/blob_id 为 null。
    let e = op_ledger_entry(&json!({"op": "set", "seq": 3, "msg": null})).expect("set 应产生实录");
    assert_eq!(e["op"], "set");
    assert_eq!(e["seq"], 3);
    assert!(e["message_id"].is_null());
    assert!(e["blob_id"].is_null());
}
