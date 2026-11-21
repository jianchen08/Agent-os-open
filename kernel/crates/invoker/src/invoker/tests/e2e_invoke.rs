// @feature: FP-0.2.一 插件协议 | @ci: rust-test
//! 端到端调用（invoke_tool/pipeline sidecar/lifecycle roundtrip/bridge）。

use super::*;
#[test]
fn sweep_freeze_dump_dir_noop_under_limit_or_missing_dir() {
    // 未超限：一个都不删
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("freeze_dumps");
    std::fs::create_dir_all(&dir).unwrap();
    for i in 0..3u64 {
        std::fs::write(dir.join(format!("d_{i}.txt")), "dump").unwrap();
    }
    super::PluginInvokerImpl::sweep_freeze_dump_dir(&dir, 10);
    assert_eq!(std::fs::read_dir(&dir).unwrap().count(), 3, "未超限零删除");

    // 目录不存在：静默返回不 panic
    super::PluginInvokerImpl::sweep_freeze_dump_dir(&tmp.path().join("nope"), 10);
}
#[tokio::test]
async fn invoke_tool_sidecar_success_wraps_business_data() {
    // tools/call 返回纯业务 dict → 归一层包 success 信封（attempt_sidecar_tool
    // Ok 主路径的端到端）；调用结束 in-flight 计数必须回落（RAII guard 随
    // future 完成释放）。
    let url =
        spawn_mock_http_mcp(r#"{"result":[1,2]}"#.to_string(), Duration::from_millis(0)).await;
    let loader = Arc::new(MockLoader::new());
    let mut m = make_http_manifest("tool_ok", &url);
    m.capabilities.tools = vec![p12_tool_cap("biz", None)];
    loader.add_manifest(m);
    let invoker = PluginInvokerImpl::new(loader);

    let r = invoker
        .invoke_tool("tool_ok", "biz", &json!({"x": 1}))
        .await
        .expect("业务 dict 必须归一为 success");
    assert!(r.success, "got: {r:?}");
    assert_eq!(r.data, json!({"result":[1,2]}), "业务数据原样进 data");
    assert_eq!(r.error, None);
    assert_eq!(r.duration_ms, None, "非信封形态不得伪造 duration_ms");
    assert_eq!(
        invoker.host_inflight("plugin:tool_ok"),
        0,
        "调用结束后 in-flight 必须归零"
    );
}
#[tokio::test]
async fn invoke_tool_sidecar_attaches_per_call_config() {
    // 工具/服务路径每调用 config 注入（2026-09-28 批次 B 补刀）：内部 sidecar
    // 的 tools/call 参数必须附带 manifest 命名空间注入配置——SDK 在调用分发
    // 单点按 arguments["config"] 做配置变更感知，缺失则插件配置冻结在握手
    // 快照（装机版新模型 no healthy deployments 事故根因）。原入参保留。
    let captured: Arc<tokio::sync::Mutex<Vec<Value>>> =
        Arc::new(tokio::sync::Mutex::new(Vec::new()));
    let url = spawn_mock_http_mcp_with_capture(
        r#"{"result":"ok"}"#.to_string(),
        Duration::from_millis(0),
        Some(captured.clone()),
        false,
    )
    .await;
    let loader = Arc::new(MockLoader::new());
    let mut m = make_http_manifest("tool_cfg", &url);
    m.capabilities.tools = vec![p12_tool_cap("biz", None)];
    m.config_files = llm_config_files();
    loader.add_manifest(m);
    let invoker = PluginInvokerImpl::new(loader);

    let r = invoker
        .invoke_tool("tool_cfg", "biz", &json!({"x": 1}))
        .await
        .expect("注入不得影响调用成功");
    assert!(r.success, "got: {r:?}");

    let calls = captured.lock().await;
    let args = calls
        .iter()
        .find_map(|p| p.get("arguments"))
        .expect("mock 必须捕获到 tools/call arguments");
    assert_eq!(args["x"], 1, "原入参键保留");
    assert_eq!(
        args["config"]["llm"]["chat"], "minimax-m3",
        "每调用注入配置必须以 config 键附带（SDK 配置变更感知的输入）"
    );
}
#[tokio::test]
async fn invoke_tool_external_mcp_arguments_carry_no_config() {
    // 区分度对照：外部 MCP（entry=mcp:external）走 sanitize 分支，不注入
    // 配置——注入配置含凭证不得出境。
    let captured: Arc<tokio::sync::Mutex<Vec<Value>>> =
        Arc::new(tokio::sync::Mutex::new(Vec::new()));
    let url = spawn_mock_http_mcp_with_capture(
        r#"{"result":"ok"}"#.to_string(),
        Duration::from_millis(0),
        Some(captured.clone()),
        false,
    )
    .await;
    let loader = Arc::new(MockLoader::new());
    let mut m = make_http_manifest("tool_ext", &url);
    m.entry = "mcp:external".to_string();
    m.capabilities.tools = vec![p12_tool_cap("biz", None)];
    m.config_files = llm_config_files();
    loader.add_manifest(m);
    let invoker = PluginInvokerImpl::new(loader);

    let r = invoker
        .invoke_tool("tool_ext", "biz", &json!({"x": 1}))
        .await
        .expect("外部分支不受影响");
    assert!(r.success, "got: {r:?}");

    let calls = captured.lock().await;
    let args = calls
        .iter()
        .find_map(|p| p.get("arguments"))
        .expect("mock 必须捕获到 tools/call arguments");
    assert!(
        args.get("config").is_none(),
        "外部 MCP 参数不得携带注入配置，got: {args}"
    );
    assert_eq!(args["x"], 1, "业务入参照常透传");
}
#[tokio::test]
async fn invoke_tool_sidecar_failure_envelope_emits_on_error() {
    // tools/call 返回 {success:false, error} 失败信封 → Ok 携带 failure（不改
    // 错误处理语义），且中央返回处旁路广播 OnError（tool 形态业务失败也计数，
    // code=None → ctx 无 error_code 键）。
    let url = spawn_mock_http_mcp(
        r#"{"success":false,"error":"boom"}"#.to_string(),
        Duration::from_millis(0),
    )
    .await;
    let loader = Arc::new(MockLoader::new());
    let mut m = make_http_manifest("tool_fail_emit", &url);
    m.capabilities.tools = vec![p12_tool_cap("biz", None)];
    loader.add_manifest(m);
    let invoker = PluginInvokerImpl::new(loader);
    let bus = Arc::new(HookEventBus::new(32));
    invoker.set_hook_bus(bus.clone());
    let mut rx = bus.subscribe();

    let r = invoker
        .invoke_tool("tool_fail_emit", "biz", &json!({}))
        .await
        .expect("失败信封走 Ok 通道，不转 Err");
    assert!(!r.success);
    assert_eq!(r.error.as_deref(), Some("boom"));

    let ev_load = tokio::time::timeout(std::time::Duration::from_millis(500), rx.recv())
        .await
        .expect("应收到 OnLoad 事件")
        .expect("总线未关闭");
    assert_eq!(
        ev_load.hook,
        LifecycleHook::OnLoad,
        "sidecar spawn 握手完成的旁路 OnLoad 广播先于调用"
    );
    let ev = tokio::time::timeout(std::time::Duration::from_millis(500), rx.recv())
        .await
        .expect("应收到 OnError 事件")
        .expect("总线未关闭");
    assert_eq!(ev.hook, LifecycleHook::OnError);
    assert_eq!(ev.ctx.get("plugin_id"), Some(&json!("tool_fail_emit")));
    assert_eq!(ev.ctx.get("error"), Some(&json!("boom")));
    assert!(
        ev.ctx.get("error_code").is_none(),
        "tool 形态 success=false 无错误码，不得伪造"
    );
}
#[tokio::test]
async fn invoke_pipeline_plugin_sidecar_success_parses_plugin_result() {
    // tools/call（call_tool_raw_fragments）返回合法 PluginResult JSON →
    // attempt_sidecar_pipeline Ok 主路径端到端：state_updates 原样返回。
    let url = spawn_mock_http_mcp(
        r#"{"state_updates":{"approved":true},"skip_remaining":false}"#.to_string(),
        Duration::from_millis(0),
    )
    .await;
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_mock_http_pipeline_manifest("pipe_ok", &url));
    let invoker = PluginInvokerImpl::new(loader);
    let state = json!({"pipeline_id": "p-1"});
    let ctx = PluginContext::new(
        &state,
        json!({}),
        TenantContext::new("t1", "s1"),
        Uuid::new_v4(),
        agentos_core::types::ContentLoader::new(
            Arc::new(MockStorage),
            "run1".to_string(),
            "main".to_string(),
        ),
    );

    let pr = invoker
        .invoke_pipeline_plugin("pipe_ok", &ctx)
        .await
        .expect("合法 PluginResult 必须成功解析");
    assert_eq!(pr.state_updates.get("approved"), Some(&json!(true)));
    assert!(!pr.skip_remaining);
    assert!(pr.error.is_none());
    assert_eq!(
        invoker.host_inflight("plugin:pipe_ok"),
        0,
        "管道调用结束后 in-flight 必须归零"
    );
}
#[tokio::test]
async fn invoke_pipeline_plugin_unparseable_payload_is_parse_error() {
    // content[0].text 是合法 JSON 但不是 PluginResult 形状（数组）→
    // PARSE_ERROR（结果序列化错误分支），且非死亡类失败不触发重试。
    let url = spawn_mock_http_mcp("[1,2,3]".to_string(), Duration::from_millis(0)).await;
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_mock_http_pipeline_manifest("pipe_bad_parse", &url));
    let invoker = PluginInvokerImpl::new(loader);
    let state = json!({});
    let ctx = PluginContext::new(
        &state,
        json!({}),
        TenantContext::new("t1", "s1"),
        Uuid::new_v4(),
        agentos_core::types::ContentLoader::new(
            Arc::new(MockStorage),
            "run1".to_string(),
            "main".to_string(),
        ),
    );

    let err = invoker
        .invoke_pipeline_plugin("pipe_bad_parse", &ctx)
        .await
        .expect_err("非 PluginResult 形状必须报 PARSE_ERROR");
    assert_eq!(err.code.as_deref(), Some("PARSE_ERROR"), "got: {err:?}");
}
#[tokio::test]
async fn invoke_pipeline_plugin_iserror_envelope_becomes_plugin_error() {
    // SDK isError 出口（isError=true + content[0].text=JSON 失败信封字符串）在
    // 管道解析点必须落成结构化 PluginResult{error}，不得卡 PARSE_ERROR
    // （invalid type: string——PluginResult.error 是 PluginError 结构体，
    // 2026-11-18 管道 7fb38fe8 实证：LLM 上游故障丢结构化错误）。
    // error 文本取信封内层 error 字段（剥 SDK JSON 外壳）。
    let envelope = r#"{"success": false, "error": "原错误: RuntimeError: llm.complete_stream 工具执行失败: upstream 502"}"#;
    let url = spawn_mock_http_mcp_is_error(envelope.to_string(), Duration::from_millis(0)).await;
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_mock_http_pipeline_manifest("pipe_iserr", &url));
    let invoker = PluginInvokerImpl::new(loader);
    let state = json!({});
    let ctx = PluginContext::new(
        &state,
        json!({}),
        TenantContext::new("t1", "s1"),
        Uuid::new_v4(),
        agentos_core::types::ContentLoader::new(
            Arc::new(MockStorage),
            "run1".to_string(),
            "main".to_string(),
        ),
    );

    let pr = invoker
        .invoke_pipeline_plugin("pipe_iserr", &ctx)
        .await
        .expect("isError 信封必须落成结构化 PluginResult 而非 PARSE_ERROR");
    let err = pr.error.expect("PluginResult.error 必须携带");
    assert!(
        err.message.contains("llm.complete_stream 工具执行失败"),
        "错误文本必须保留根因: {err:?}"
    );
    assert!(
        !err.message.starts_with('{'),
        "必须剥掉 SDK JSON 信封外壳: {err:?}"
    );
    assert!(pr.state_updates.is_empty(), "失败步骤不得携带状态更新");
    assert!(!pr.skip_remaining);
}
#[tokio::test]
async fn invoke_pipeline_plugin_iserror_plain_text_kept_verbatim() {
    // isError=true 但 text 非 JSON（裸文案）→ 回退原文，不丢信息。
    let url =
        spawn_mock_http_mcp_is_error("sidecar exploded".to_string(), Duration::from_millis(0))
            .await;
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_mock_http_pipeline_manifest("pipe_iserr_plain", &url));
    let invoker = PluginInvokerImpl::new(loader);
    let state = json!({});
    let ctx = PluginContext::new(
        &state,
        json!({}),
        TenantContext::new("t1", "s1"),
        Uuid::new_v4(),
        agentos_core::types::ContentLoader::new(
            Arc::new(MockStorage),
            "run1".to_string(),
            "main".to_string(),
        ),
    );

    let pr = invoker
        .invoke_pipeline_plugin("pipe_iserr_plain", &ctx)
        .await
        .expect("裸文案 isError 同样必须落成结构化 PluginResult");
    let err = pr.error.expect("PluginResult.error 必须携带");
    assert_eq!(err.message, "sidecar exploded", "got: {err:?}");
    assert!(pr.state_updates.is_empty());
    assert!(!pr.skip_remaining);
}
#[tokio::test]
async fn invoke_tool_capability_timeout_enforced_end_to_end() {
    // P12 端到端：工具声明 timeout_ms=300，tools/call 响应延迟 5s →
    // invoke_tool 返回结构化 CAPABILITY_TIMEOUT（message 含插件/工具/时长），
    // 超时放弃内层 future 后 in-flight 计数随 drop 回落。
    let url = spawn_mock_http_mcp(r#"{"ok":true}"#.to_string(), Duration::from_secs(5)).await;
    let loader = Arc::new(MockLoader::new());
    let mut m = make_http_manifest("slow_tool", &url);
    m.capabilities.tools = vec![p12_tool_cap("slow", Some(300))];
    loader.add_manifest(m);
    let invoker = PluginInvokerImpl::new(loader);

    let err = invoker
        .invoke_tool("slow_tool", "slow", &json!({}))
        .await
        .expect_err("到点必须放弃内层调用");
    assert_eq!(err.code.as_deref(), Some("CAPABILITY_TIMEOUT"));
    assert!(
        err.message.contains("slow_tool"),
        "message 须含插件 id: {err}"
    );
    assert!(err.message.contains("slow"), "message 须含工具名: {err}");
    assert!(err.message.contains("300ms"), "message 须含时长: {err}");
    assert_eq!(
        invoker.host_inflight("plugin:slow_tool"),
        0,
        "超时放弃内层 future 后 in-flight 必须回落"
    );
}
#[tokio::test]
async fn list_plugin_tools_reclaims_freshly_spawned_host_without_hooks() {
    // G2 主路径：本次新 spawn 的宿主（缓存原本无条目）校验完回收——kill +
    // 移除缓存，懒加载语义不被破坏。
    let url = spawn_mock_http_mcp(r#"{"ok":true}"#.to_string(), Duration::from_millis(0)).await;
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_http_manifest("g2_reclaim", &url));
    let invoker = PluginInvokerImpl::new(loader);

    let raw = invoker
        .list_plugin_tools("g2_reclaim")
        .await
        .expect("tools/list 应成功");
    assert!(
        raw["tools"].is_array(),
        "tools/list 应返回规范 ListToolsResult: {raw}"
    );
    assert!(
        invoker
            .mcp_clients
            .read()
            .get("plugin:g2_reclaim")
            .is_none(),
        "新 spawn 宿主校验后必须回收（缓存条目移除）"
    );
}
#[tokio::test]
async fn list_plugin_tools_keeps_freshly_spawned_host_with_lifecycle_hooks() {
    // G2 例外分支：声明 lifecycle_hooks 的插件 on_load 有副作用（起子进程/
    // 绑端口），探测完 kill 会毁掉初始化成果——此类新 spawn 宿主不回收，交
    // idle GC 空闲回收。
    let url = spawn_mock_http_mcp(r#"{"ok":true}"#.to_string(), Duration::from_millis(0)).await;
    let loader = Arc::new(MockLoader::new());
    let mut m = make_http_manifest("g2_keep_hooks", &url);
    m.capabilities.lifecycle_hooks = vec![LifecycleHook::OnLoad];
    loader.add_manifest(m);
    let invoker = PluginInvokerImpl::new(loader);

    let raw = invoker
        .list_plugin_tools("g2_keep_hooks")
        .await
        .expect("tools/list 应成功");
    assert!(
        raw["tools"].is_array(),
        "tools/list 应返回规范 ListToolsResult: {raw}"
    );
    assert!(
        invoker
            .mcp_clients
            .read()
            .get("plugin:g2_keep_hooks")
            .is_some(),
        "声明 lifecycle_hooks 的新 spawn 宿主不得被回收"
    );
}
#[tokio::test]
async fn list_plugin_tools_non_sidecar_errors() {
    // 仅 sidecar 支持 G2 describe；native（InProcess）直接报不支持（api 层
    // 据此跳过校验）。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_inprocess_manifest("g2_native"));
    let invoker = PluginInvokerImpl::new(loader);

    let err = invoker
        .list_plugin_tools("g2_native")
        .await
        .expect_err("非 sidecar 必须报不支持");
    assert!(err.message.contains("InProcess"), "got: {}", err.message);
}
#[tokio::test]
async fn set_router_invalidates_cached_sidecars_and_kills_processes() {
    // set_router 契约：缓存客户端的 initialize capabilities 是旧 router 快照
    // 且永不重连——注入 router 时必须全量废弃（kill + drain 缓存 + 清 spawn
    // 成员集快照），下次调用 respawn 拿新 capabilities。
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    let live = spawn_long_lived_stdio_client().await;
    assert!(live.is_alive().await, "前置：长驻假 sidecar 必须存活");
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert("plugin:stale_router".to_string(), Arc::clone(&live_arc));
    invoker.spawned_members.write().insert(
        "plugin:stale_router".to_string(),
        vec!["stale_router".to_string()],
    );

    invoker.set_router(Arc::new(RecordRouter {
        calls: Arc::new(std::sync::Mutex::new(Vec::new())),
    }));

    assert!(
        invoker.mcp_clients.read().is_empty(),
        "set_router 后缓存必须全量 drain"
    );
    assert!(
        invoker.spawned_members.read().is_empty(),
        "spawn 成员集快照与缓存条目同生命周期，必须一并清空"
    );
    // tokio runtime 内：异步 kill 任务已 spawn，进程最终被杀
    wait_until_killed(&live_arc, "被废弃的 sidecar 进程必须被 kill").await;
}
#[tokio::test]
async fn invalidate_all_sidecars_drains_cached_hosts_and_kills_processes() {
    // 缺陷E（台账 R2）boot 竞态收口原语：chat.send_message 注册晚于部分宿主
    // 的 initialize 握手（快照缺 chat，终身缺句柄）——注册完成后必须全量
    // 废弃缓存宿主（kill + drain 缓存 + 清 spawn 成员集快照），下次调用
    // respawn 按新声明面重握手。语义与 set_router 的废弃面一致，但不动
    // router 本体（注册发生在既有 registry 上）。
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    let live = spawn_long_lived_stdio_client().await;
    assert!(live.is_alive().await, "前置：长驻假 sidecar 必须存活");
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert("plugin:boot_window_host".to_string(), Arc::clone(&live_arc));
    invoker.spawned_members.write().insert(
        "plugin:boot_window_host".to_string(),
        vec!["boot_window_host".to_string()],
    );
    invoker
        .pending_rejoin_members
        .write()
        .entry("plugin:boot_window_host".to_string())
        .or_default()
        .insert("late_member".to_string());

    invoker.invalidate_all_sidecars("test: chat capability registered");

    assert!(
        invoker.mcp_clients.read().is_empty(),
        "invalidate_all_sidecars 后缓存必须全量 drain"
    );
    assert!(
        invoker.spawned_members.read().is_empty(),
        "spawn 成员集快照与缓存条目同生命周期，必须一并清空"
    );
    assert!(
        invoker.pending_rejoin_members.read().is_empty(),
        "待重加入集与缓存条目同生命周期，必须一并清空"
    );
    // tokio runtime 内：异步 kill 任务已 spawn，进程最终被杀
    wait_until_killed(&live_arc, "被废弃的 sidecar 进程必须被 kill").await;
}
#[tokio::test]
async fn send_lifecycle_hook_delivery_failures_still_ok() {
    // fire-and-forget 语义：钩子投递失败（sidecar 客户端创建失败 / native 无
    // 通知通道）只留痕不阻断——send_lifecycle_hook 恒 Ok。
    let loader = Arc::new(MockLoader::new());
    // 非 python 裸命令 → 不走 venv fail-closed，connect 时 spawn 失败
    loader.add_manifest(make_sidecar_manifest(
        "hook_spawn_fail",
        "definitely_missing_command_98765",
    ));
    loader.add_manifest(make_inprocess_manifest("hook_native"));
    let invoker = PluginInvokerImpl::new(loader);
    let ctx = HookContext::new();

    let sidecar = invoker
        .send_lifecycle_hook("hook_spawn_fail", LifecycleHook::OnLoad, &ctx)
        .await;
    assert!(
        sidecar.is_ok(),
        "sidecar 客户端创建失败仅 warn: {sidecar:?}"
    );

    let native = invoker
        .send_lifecycle_hook("hook_native", LifecycleHook::OnUnload, &ctx)
        .await;
    assert!(native.is_ok(), "native 无通知通道 no-op Ok: {native:?}");
}
#[tokio::test]
async fn invoke_pipeline_native_missing_plugin_dir_errors() {
    // native pipeline 路径第二道校验：manifest.native.artifact 在但 loader 查
    // 不到插件目录 → NATIVE_PLUGIN_DIR_NOT_FOUND（未发现/已卸载）。
    let loader = Arc::new(MockLoader::new());
    let mut m = make_inprocess_manifest("native_pipe_no_dir");
    m.native = Some(agentos_core::traits::NativeArtifact {
        artifact: "whatever.dll".to_string(),
        pre_dispatch: None,
    });
    loader.add_manifest(m);
    let invoker =
        PluginInvokerImpl::new(loader).set_native_loader(Arc::new(NativePluginLoader::new()));
    let state = json!({});
    let ctx = PluginContext::new(
        &state,
        json!({}),
        TenantContext::new("t1", "s1"),
        Uuid::new_v4(),
        agentos_core::types::ContentLoader::new(
            Arc::new(MockStorage),
            "run1".to_string(),
            "main".to_string(),
        ),
    );

    let err = invoker
        .invoke_pipeline_plugin("native_pipe_no_dir", &ctx)
        .await
        .expect_err("查不到插件目录必须报错");
    assert_eq!(err.code.as_deref(), Some("NATIVE_PLUGIN_DIR_NOT_FOUND"));
}
#[test]
fn notify_if_crash_matches_only_panic_and_fatal_codes() {
    // 崩溃回调语义：Err 且错误码含 PANICKED/FATAL 才通知；其他错误码、无码、
    // Ok 一律不通知（native pipeline/tool 共用的中央判定）。
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    let fired = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
    let fired_clone = Arc::clone(&fired);
    invoker.on_crash(Arc::new(move |plugin_id: &str| {
        fired_clone.lock().unwrap().push(plugin_id.to_string());
    }));
    let pe = |code: Option<&str>| PluginError {
        message: "x".to_string(),
        code: code.map(str::to_string),
        source: None,
    };

    invoker.notify_if_crash::<PluginResult>("p_panic", &Err(pe(Some("NATIVE_EXECUTE_PANICKED"))));
    invoker.notify_if_crash::<PluginResult>("p_fatal", &Err(pe(Some("EXECUTE_FATAL"))));
    invoker.notify_if_crash::<PluginResult>("p_load", &Err(pe(Some("NATIVE_LOAD_FAILED"))));
    invoker.notify_if_crash::<PluginResult>("p_nocode", &Err(pe(None)));
    invoker.notify_if_crash::<PluginResult>("p_ok", &Ok(PluginResult::default()));

    assert_eq!(
        *fired.lock().unwrap(),
        vec!["p_panic".to_string(), "p_fatal".to_string()],
        "只有 PANICKED/FATAL 触发崩溃回调"
    );
}
#[test]
fn normalize_mcp_tool_result_envelope_with_non_bool_success_is_parse_error() {
    // ②-a 真 ToolExecutionResult 信封（同时带 success + data）但 success 非
    // bool → from_value 失败 → PARSE_ERROR（与 ②-b 同判，禁止成功偏置）。
    let err = normalize_mcp_tool_result(
        json!({"success": "yes", "data": {"k": "v"}}),
        "drifting_tool",
    )
    .unwrap_err();
    assert_eq!(err.code.as_deref(), Some("PARSE_ERROR"));
    assert!(
        err.message
            .contains("failed to parse MCP response as ToolExecutionResult"),
        "报错应指明信封解析失败: {err}"
    );
}
#[test]
fn compute_plugin_fingerprint_filters_junk_and_includes_config_files() {
    // 指纹输入面：.log 扩展 / 隐藏文件 / 子目录（__pycache__）不进指纹；
    // manifest.config_files 声明的配置文件（可在插件目录外）进指纹——mtime
    // 变化触发变化；文件消失按缺失降级（"0:0"）也产生变化。
    let tmp = tempfile::tempdir().unwrap();
    std::fs::write(tmp.path().join("server.py"), b"print(1)").unwrap();
    let cfg = tmp.path().join("llm.yaml");
    std::fs::write(&cfg, b"model: gpt\n").unwrap();
    let mut manifest = make_sidecar_manifest("fp_junk", "python server.py");
    manifest.config_files = vec![agentos_core::traits::ConfigFileMapping {
        id: "llm".to_string(),
        settings: None,
        path: cfg.to_string_lossy().into_owned(),
        label: "llm".to_string(),
        target: None,
        fields: vec![],
    }];
    let fp_base = compute_plugin_fingerprint(tmp.path(), &manifest);

    // 运行时杂物：诊断日志、隐藏 yaml、子目录产物——都不得改变指纹
    std::fs::write(tmp.path().join("diag.log"), b"log").unwrap();
    std::fs::write(tmp.path().join(".secret.yaml"), b"k: v").unwrap();
    std::fs::create_dir_all(tmp.path().join("__pycache__")).unwrap();
    std::fs::write(
        tmp.path()
            .join("__pycache__")
            .join("server.cpython-312.pyc"),
        b"x",
    )
    .unwrap();
    assert_eq!(
        compute_plugin_fingerprint(tmp.path(), &manifest),
        fp_base,
        "杂物（.log/隐藏/子目录）不得进指纹，否则 sidecar 写临时文件会误触发 respawn"
    );

    // 仅配置文件内容变化（源码未动）→ 指纹必须变化（config_files 纳入）
    std::thread::sleep(std::time::Duration::from_secs_f64(1.1));
    std::fs::write(&cfg, b"model: gpt-5\n").unwrap();
    let fp_cfg_changed = compute_plugin_fingerprint(tmp.path(), &manifest);
    assert_ne!(fp_cfg_changed, fp_base, "config_files 声明的文件必须进指纹");

    // 配置文件消失 → 缺失降级（"0:0"）同样改变指纹
    std::fs::remove_file(&cfg).unwrap();
    assert_ne!(
        compute_plugin_fingerprint(tmp.path(), &manifest),
        fp_cfg_changed,
        "配置文件消失必须产生指纹变化（触发 respawn 暴露缺配置）"
    );
}
#[tokio::test]
async fn is_plugin_stale_flags_change_only_after_ttl_window() {
    // Pull 热加载四分支：首次写入指纹不算过期；TTL 内短路；TTL 过期 + 指纹
    // 未变 → false（刷新检测时刻）；TTL 过期 + 指纹变 → true 并更新缓存。
    let tmp = tempfile::tempdir().unwrap();
    std::fs::write(tmp.path().join("server.py"), b"print(1)").unwrap();
    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "stale_ttl_full".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    let manifest = make_sidecar_manifest("stale_ttl_full", "python server.py");
    let invoker = PluginInvokerImpl::new(loader);
    let backdate = |invoker: &PluginInvokerImpl| {
        let (fp, _) = *invoker
            .fingerprints
            .read()
            .get("stale_ttl_full")
            .expect("首次检测应写入指纹缓存");
        invoker.fingerprints.write().insert(
            "stale_ttl_full".to_string(),
            (fp, Instant::now() - Duration::from_secs(2)),
        );
    };

    assert!(
        !invoker.is_plugin_stale("stale_ttl_full", &manifest).await,
        "首次不算过期"
    );
    backdate(&invoker);
    assert!(
        !invoker.is_plugin_stale("stale_ttl_full", &manifest).await,
        "TTL 过期但指纹未变 → 未过期"
    );
    std::thread::sleep(std::time::Duration::from_secs_f64(1.1));
    std::fs::write(tmp.path().join("server.py"), b"print(2) # changed").unwrap();
    backdate(&invoker);
    assert!(
        invoker.is_plugin_stale("stale_ttl_full", &manifest).await,
        "TTL 过期 + 指纹变 → 已过期（触发 respawn）"
    );
    assert!(
        !invoker.is_plugin_stale("stale_ttl_full", &manifest).await,
        "过期检测刷新时刻后，TTL 内立即再查应短路为 false"
    );
}
#[tokio::test]
async fn is_host_stale_solo_detects_change_after_ttl() {
    // 宿主级过期检测（独占宿主 = 插件自身指纹）：首次写入不算过期；TTL 内
    // 短路；TTL 过期 + 代码变更 → true（fast path 据此 kill respawn）。
    let tmp = tempfile::tempdir().unwrap();
    std::fs::write(tmp.path().join("server.py"), b"print(1)").unwrap();
    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "solo_stale".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    let manifest = make_sidecar_manifest("solo_stale", "python server.py");
    let invoker = PluginInvokerImpl::new(loader);
    let host_key = solo_host_key("solo_stale");

    assert!(
        !invoker.host_code_stale(&host_key, &manifest).await,
        "首次不算过期"
    );
    assert!(
        !invoker.host_code_stale(&host_key, &manifest).await,
        "TTL 内短路（不 stat 文件系统）"
    );
    std::thread::sleep(std::time::Duration::from_secs_f64(1.1));
    std::fs::write(tmp.path().join("server.py"), b"print(2) # changed").unwrap();
    // 回写 2s 前绕过 TTL 门（不真实等待）
    let (fp, _) = *invoker.fingerprints.read().get(&host_key).unwrap();
    invoker.fingerprints.write().insert(
        host_key.clone(),
        (fp, Instant::now() - Duration::from_secs(2)),
    );
    assert!(
        invoker.host_code_stale(&host_key, &manifest).await,
        "TTL 过期 + 代码变更 → 宿主过期"
    );
}
#[tokio::test]
async fn light_host_missing_spawn_snapshot_rebuilds_cached_host() {
    // 漂移检测 fail-closed 分支（BUG-44 契约反转）：light 宿主缓存条目无
    // spawn 成员集快照 = 进程成员集未知——复用未知成员集的进程正是 BUG-44
    // T1 复用旧宿主报 MCP [-32602] 的形态，必须按漂移处理整宿主重建
    // （respawn 按当前分配表重写快照，自愈有界），不得保守复用。
    // 前契约（保守不杀，本测试旧名 light_host_missing_spawn_snapshot_reuses_
    // cached_client）建立在「生产路径缓存条目必带快照」的不变量上；该不变量
    // 被快路径驱逐/respawn 竞态打破（见
    // test_blocked_eviction_cannot_delete_concurrently_respawned_snapshot）。
    let loader = Arc::new(MockLoader::new());
    let manifest = make_light_manifest("snap_missing", "python server.py");
    loader.add_manifest(manifest.clone());
    let invoker = PluginInvokerImpl::new(loader);

    let host_key = invoker.resolve_host_key(&manifest);
    assert_eq!(host_key, "group:light:1");
    let live = spawn_long_lived_stdio_client().await;
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));
    // 刻意不写 spawned_members 快照

    // respawn 被触发：host 命令解析先于 spawn 失败（_host 未落地，
    // HOST_DIR_NOT_FOUND）——不得返回缓存实例（成员集未知）
    let err = match invoker.get_or_create_mcp_client(&manifest).await {
        Err(e) => e,
        Ok(_) => panic!("快照缺失（成员集未知）必须整宿主 respawn，不得复用缓存实例"),
    };
    assert_eq!(
        err.code.as_deref(),
        Some("HOST_DIR_NOT_FOUND"),
        "respawn 路径证据：{err}"
    );
    wait_until_killed(&live_arc, "成员集未知的宿主必须 kill 重建").await;
    assert!(invoker.mcp_clients.read().get(&host_key).is_none());
}
#[tokio::test]
async fn collect_plugin_roots_parents_of_manifest_dirs_via_discover() {
    // discover_new_plugins 的 roots 收集：从 AGENTOS_PLUGINS_DIR 递归找含
    // plugin.json 的目录（含无 manifest 的中间目录下钻），取父目录去重作为
    // discover roots（支持 tools/<plugin>/ 嵌套布局）。
    let tmp = tempfile::tempdir().unwrap();
    let base = tmp.path();
    std::fs::create_dir_all(base.join("tools").join("demo")).unwrap();
    std::fs::write(base.join("tools").join("demo").join("plugin.json"), b"{}").unwrap();
    // 无 manifest 的中间目录下钻：helper/ 自身无 plugin.json，但 inner/ 有
    std::fs::create_dir_all(base.join("tools").join("helper").join("inner")).unwrap();
    std::fs::write(
        base.join("tools")
            .join("helper")
            .join("inner")
            .join("plugin.json"),
        b"{}",
    )
    .unwrap();
    std::fs::create_dir_all(base.join("other")).unwrap();
    std::fs::write(base.join("other").join("plugin.json"), b"{}").unwrap();

    let _env = EnvVarGuard::set("AGENTOS_PLUGINS_DIR", base.to_string_lossy().as_ref());
    // 用户插件根同样进 roots（collect_plugin_roots 双根）：钉到空临时目录，
    // 否则会读到宿主机真实用户空间里的已装插件，期望集合随环境漂移。
    let empty_user_plugins = tempfile::tempdir().unwrap();
    let _user_env = EnvVarGuard::set(
        agentos_core::user_space::USER_PLUGINS_DIR_ENV,
        empty_user_plugins.path().to_string_lossy().as_ref(),
    );
    let loader = Arc::new(MockLoader::new());
    let invoker: Arc<dyn PluginInvoker> = Arc::new(PluginInvokerImpl::new(loader.clone()));
    let found = invoker
        .discover_new_plugins()
        .await
        .expect("discover 应成功");
    assert!(found.is_empty(), "MockLoader 无 manifest，发现 0 个");

    let mut roots = loader
        .captured_roots
        .read()
        .last()
        .expect("roots 已捕获")
        .clone();
    roots.sort();
    // other/plugin.json 的插件目录是 base/other，其父目录 = base 本身
    let expected: Vec<String> = [
        base.to_path_buf(),
        base.join("tools"),
        base.join("tools").join("helper"),
    ]
    .iter()
    .map(|p| p.to_string_lossy().into_owned())
    .collect();
    assert_eq!(roots, expected, "roots = manifest 目录的父目录去重集合");
}
