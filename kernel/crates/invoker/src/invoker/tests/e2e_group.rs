// @feature: FP-0.2.一 插件协议 | @ci: rust-test
//! 端到端合宿与键域（jsonrpc/mcp_client/respawn/group key/fingerprint edges）。

use super::*;
#[test]
fn bridge_timeout_ms_declared_wins_else_kernel_default() {
    // 等待界解析序：工具声明 timeout_ms > 插件级 request_timeout_secs >
    // 内核默认（既有 tool_timeout_ms 序的桥面复用）；豁免哨兵在桥面回落
    // 默认界（护栏非业务超时）；注册表反查路径/非 tool-executor → 默认界。
    let loader = MockLoader::new();

    // ① 工具声明 timeout_ms 优先
    let mut m1 = make_sidecar_manifest("callee_t1", "python server.py");
    m1.capabilities.tools = vec![p12_tool_cap("long_tool", Some(12_345))];
    loader.add_manifest(m1);
    let p1 = json!({"tool_name": "long_tool", "plugin_id": "callee_t1", "args": {}});
    assert_eq!(
        bridge_timeout_ms(&loader, "tool-executor", "invoke", &p1),
        12_345,
        "被调工具声明 timeout_ms 应优先"
    );

    // ② 未声明工具级 → 插件级 request_timeout_secs 兜底（既有解析序中位）
    let mut m2 = make_sidecar_manifest("callee_t2", "python server.py");
    m2.capabilities.tools = vec![p12_tool_cap("plain_tool", None)];
    m2.mcp = Some(McpConfig {
        allowed_roots: Vec::new(),
        transport: McpTransport::Stdio,
        endpoint: None,
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: Some(7),
    });
    loader.add_manifest(m2);
    let p2 = json!({"tool_name": "plain_tool", "plugin_id": "callee_t2", "args": {}});
    assert_eq!(
        bridge_timeout_ms(&loader, "tool-executor", "invoke", &p2),
        7_000,
        "插件级 request_timeout_secs 应按既有解析序兜底"
    );

    // ③ 豁免哨兵（Some(0)）在桥面回落默认界：桥面界是挂死护栏，
    //    业务豁免不重开无界驻留
    let mut m3 = make_sidecar_manifest("callee_t3", "python server.py");
    m3.capabilities.tools = vec![p12_tool_cap("exempt_tool", Some(0))];
    loader.add_manifest(m3);
    let p3 = json!({"tool_name": "exempt_tool", "plugin_id": "callee_t3", "args": {}});
    assert_eq!(
        bridge_timeout_ms(&loader, "tool-executor", "invoke", &p3),
        default_capability_timeout_ms(),
        "豁免哨兵在桥面回落内核默认界"
    );

    // ④ 无显式 plugin_id（tool_core 注册表反查路径）→ 桥面同步不可解析 → 默认界
    let p4 = json!({"tool_name": "long_tool", "args": {}});
    assert_eq!(
        bridge_timeout_ms(&loader, "tool-executor", "invoke", &p4),
        default_capability_timeout_ms(),
        "注册表反查路径取内核默认界"
    );

    // ⑤ 显式 plugin_id 但 manifest 不在册（get_manifest None）→ 默认界兜底
    let p5 = json!({"tool_name": "long_tool", "plugin_id": "callee_absent", "args": {}});
    assert_eq!(
        bridge_timeout_ms(&loader, "tool-executor", "invoke", &p5),
        default_capability_timeout_ms(),
        "被调方 manifest 不可得取内核默认界"
    );

    // ⑥ 非 tool-executor capability（event-bus 等）→ 默认界
    let p6 = json!({"channel": "x"});
    assert_eq!(
        bridge_timeout_ms(&loader, "event-bus", "emit", &p6),
        default_capability_timeout_ms(),
        "非 tool-executor capability 取内核默认界"
    );
}
#[tokio::test]
async fn double_check_reuse_returns_live_client_and_evicts_dead() {
    // spawn 锁后的 double-check 复用门：命中且存活 → touch 后返回同一实例；
    // 判死 → kill + 驱逐 + None（继续 spawn 路径）。
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));

    let live: SharedMcpClient = Arc::new(tokio::sync::RwLock::new(McpClient::new_http(
        "http://127.0.0.1:9/mcp",
        HashMap::new(),
        None,
    )));
    invoker
        .mcp_clients
        .write()
        .insert("plugin:dc_live".to_string(), Arc::clone(&live));
    let got = invoker
        .reuse_fresh_host_locked("plugin:dc_live", "dc_live")
        .await
        .expect("存活缓存应复用");
    assert!(
        Arc::ptr_eq(&got, &live),
        "double-check 必须返回同一缓存实例"
    );
    assert!(invoker.mcp_clients.read().get("plugin:dc_live").is_some());

    // 无 child 的 stdio client = 判死 → kill + 驱逐 + None
    invoker
        .mcp_clients
        .write()
        .insert("plugin:dc_dead".to_string(), unconnected_stdio_client());
    assert!(
        invoker
            .reuse_fresh_host_locked("plugin:dc_dead", "dc_dead")
            .await
            .is_none(),
        "double-check 判死必须驱逐并返回 None"
    );
    assert!(
        invoker.mcp_clients.read().get("plugin:dc_dead").is_none(),
        "死实例必须从缓存清除"
    );
}
#[tokio::test]
async fn lifecycle_hook_and_tool_call_roundtrip_over_live_stdio_sidecar() {
    // 真 stdio 替身（外部进程 = 可 mock 边界）：echo JSON-RPC 的 python 进程。
    // 覆盖 send_lifecycle_hook 的 sidecar 投递成功路径（get_or_create →
    // is_alive → 通知真实写出）与 attempt_sidecar_tool 的 stdio 通道端到端 +
    // 缓存连接复用。
    assert_python_available();
    let script = "import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        m = json.loads(line)
    except Exception:
        continue
    if isinstance(m, dict) and 'id' in m:
        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': {'ok': True}}) + '\\n')
        sys.stdout.flush()
";
    let loader = Arc::new(MockLoader::new());
    let mut manifest = make_sidecar_manifest("stdio_echo", "external");
    manifest.mcp = Some(McpConfig {
        allowed_roots: Vec::new(),
        transport: McpTransport::Stdio,
        endpoint: Some(McpEndpoint {
            command: Some(python_exe().to_string()),
            args: vec!["-u".to_string(), "-c".to_string(), script.to_string()],
            ..Default::default()
        }),
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: None,
    });
    loader.add_manifest(manifest.clone());
    let invoker = PluginInvokerImpl::new(loader);

    // ① 生命周期钩子：连接建立 + is_alive → 通知写出（Ok）
    invoker
        .send_lifecycle_hook(
            "stdio_echo",
            LifecycleHook::OnPipelineStart,
            &HookContext::new(),
        )
        .await
        .expect("存活 sidecar 的生命周期通知必须成功");
    let cached = invoker
        .mcp_clients
        .read()
        .get("plugin:stdio_echo")
        .cloned()
        .expect("钩子路径应已建立宿主缓存");

    // ② 工具调用复用同一 stdio 连接：result 无 content → 原样提取 → 纯业务
    //    数据包 success 信封
    let tr = invoker
        .invoke_tool("stdio_echo", "echo_tool", &json!({"x": 1}))
        .await
        .expect("stdio 工具调用应成功");
    assert!(tr.success, "got: {tr:?}");
    assert_eq!(tr.data["ok"], true, "替身 result 原样进 data");

    let again = invoker
        .mcp_clients
        .read()
        .get("plugin:stdio_echo")
        .cloned()
        .expect("调用后缓存条目保留");
    assert!(Arc::ptr_eq(&cached, &again), "工具调用必须复用缓存连接");

    // 清理替身进程（python 循环随 stdin 关闭退出，显式 kill 不留孤儿）
    again.write().await.kill().await.expect("清理替身进程");
}
#[tokio::test]
async fn invoke_pipeline_business_error_emits_on_error_with_code() {
    // Ok 携带 error 字段（业务失败）→ 结果照常返回，且中央返回处广播 OnError
    // （pipeline 形态 code 透传——lifecycle.plugin_error_total 的计数面）。
    let text = serde_json::json!({
        "state_updates": {}, "skip_remaining": false,
        "error": {"message": "step blew up", "code": "STEP_CODE", "source": null}
    })
    .to_string();
    let url = spawn_mock_http_mcp(text, Duration::from_millis(0)).await;
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_mock_http_pipeline_manifest("pipe_biz_err", &url));
    let invoker = PluginInvokerImpl::new(loader);
    let bus = Arc::new(HookEventBus::new(32));
    invoker.set_hook_bus(bus.clone());
    let mut rx = bus.subscribe();

    let state = json!({"pipeline_id": "p-9"});
    let ctx = make_e2e_ctx(&state);
    let pr = invoker
        .invoke_pipeline_plugin("pipe_biz_err", &ctx)
        .await
        .expect("业务失败走 Ok 通道，不转 Err");
    assert_eq!(
        pr.error.as_ref().map(|e| e.message.as_str()),
        Some("step blew up")
    );

    // OnLoad（spawn 旁路广播）之后是 OnError
    let ev_load = tokio::time::timeout(std::time::Duration::from_millis(500), rx.recv())
        .await
        .expect("应收到 OnLoad 事件")
        .expect("总线未关闭");
    assert_eq!(ev_load.hook, LifecycleHook::OnLoad);
    let ev = tokio::time::timeout(std::time::Duration::from_millis(500), rx.recv())
        .await
        .expect("应收到 OnError 事件")
        .expect("总线未关闭");
    assert_eq!(ev.hook, LifecycleHook::OnError);
    assert_eq!(ev.ctx.get("plugin_id"), Some(&json!("pipe_biz_err")));
    assert_eq!(ev.ctx.get("error"), Some(&json!("step blew up")));
    assert_eq!(
        ev.ctx.get("error_code"),
        Some(&json!("STEP_CODE")),
        "pipeline 形态业务失败必须透传错误码"
    );
}
#[tokio::test]
async fn invoke_pipeline_sidecar_jsonrpc_error_maps_to_call_failed() {
    // tools/call 回 JSON-RPC error（进程存活，HTTP 不判死）→ MCP_CALL_FAILED
    // （协议/工具错误，非死亡类，透明恢复不重试）。
    let url = spawn_mock_http_mcp_tools_call_error(-32602, "tool not found".to_string()).await;
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_mock_http_pipeline_manifest("pipe_rpc_err", &url));
    let invoker = PluginInvokerImpl::new(loader);
    let state = json!({});
    let ctx = make_e2e_ctx(&state);

    let err = invoker
        .invoke_pipeline_plugin("pipe_rpc_err", &ctx)
        .await
        .expect_err("协议错误必须转 Err");
    assert_eq!(err.code.as_deref(), Some("MCP_CALL_FAILED"), "got: {err:?}");
    assert!(
        err.message.contains("tool not found"),
        "底层协议错误信息应透传: {}",
        err.message
    );
}
#[tokio::test]
async fn invoke_tool_sidecar_jsonrpc_error_maps_to_tool_call_failed() {
    // call_tool 失败 + 存活复查（HTTP 恒活）→ MCP_TOOL_CALL_FAILED；连接
    // 保留在缓存（网络/协议错误不驱逐，下次调用复用）。
    let url = spawn_mock_http_mcp_tools_call_error(-32602, "tool not found".to_string()).await;
    let loader = Arc::new(MockLoader::new());
    let mut m = make_http_manifest("tool_rpc_err", &url);
    m.capabilities.tools = vec![p12_tool_cap("biz", None)];
    loader.add_manifest(m);
    let invoker = PluginInvokerImpl::new(loader);

    let err = invoker
        .invoke_tool("tool_rpc_err", "biz", &json!({}))
        .await
        .expect_err("协议错误必须转 Err");
    assert_eq!(
        err.code.as_deref(),
        Some("MCP_TOOL_CALL_FAILED"),
        "got: {err:?}"
    );
    assert!(
        err.message.contains("tool not found"),
        "底层协议错误信息应透传: {}",
        err.message
    );
    assert!(
        invoker
            .mcp_clients
            .read()
            .get("plugin:tool_rpc_err")
            .is_some(),
        "协议错误非进程死亡：缓存连接必须保留供复用"
    );
}
#[tokio::test]
async fn build_mcp_client_covers_transport_shapes_and_rejects_empty_command() {
    // 传输三路分流构造面：① HTTP 远程；② 外部 stdio 命令（env 解析成功路径）；
    // ③ 项目自带 sidecar（router 注入 → 反向调用包装 + working_dir + LOG_LEVEL
    // 透传）；④ 空 command → MCP_CONFIG_INVALID fail-closed。
    let tmp = tempfile::tempdir().unwrap();
    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "shape_solo".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    let invoker = PluginInvokerImpl::new(loader);
    invoker.set_router(Arc::new(RecordRouter {
        calls: Arc::new(std::sync::Mutex::new(Vec::new())),
    }));
    let _log = EnvVarGuard::set("LOG_LEVEL", "DEBUG");

    // ① HTTP 远程
    let http = make_http_manifest("shape_http", "http://127.0.0.1:9/mcp");
    invoker
        .build_mcp_client("plugin:shape_http", &http)
        .expect("HTTP 形态应可构造");

    // ② 外部 stdio 命令（含 env 解析成功路径——无占位符原样透传）
    let mut ext = make_sidecar_manifest("shape_ext", "unused");
    let mut env = std::collections::HashMap::new();
    env.insert("PLAIN_VAR".to_string(), "value".to_string());
    ext.mcp = Some(McpConfig {
        allowed_roots: Vec::new(),
        transport: McpTransport::Stdio,
        endpoint: Some(McpEndpoint {
            command: Some(python_exe().to_string()),
            args: vec!["--version".to_string()],
            env,
            ..Default::default()
        }),
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: None,
    });
    invoker
        .build_mcp_client("plugin:shape_ext", &ext)
        .expect("外部 stdio 形态应可构造");

    // ③ 项目自带 sidecar（非 python entry 免 venv 门；目录来自 loader）
    let solo = make_sidecar_manifest("shape_solo", "node server.js");
    invoker
        .build_mcp_client("plugin:shape_solo", &solo)
        .expect("自带 sidecar 形态应可构造");

    // ④ 外部 stdio 命令为空串 → MCP_CONFIG_INVALID
    let mut empty = make_sidecar_manifest("shape_empty", "unused");
    empty.mcp = Some(McpConfig {
        allowed_roots: Vec::new(),
        transport: McpTransport::Stdio,
        endpoint: Some(McpEndpoint {
            command: Some(String::new()),
            ..Default::default()
        }),
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: None,
    });
    let err = match invoker.build_mcp_client("plugin:shape_empty", &empty) {
        Err(e) => e,
        Ok(_) => panic!("空 command 必须 fail-closed，不得构造成功"),
    };
    assert_eq!(err.code.as_deref(), Some("MCP_CONFIG_INVALID"));
    assert!(
        err.message.contains("缺 command"),
        "错误应点名缺 command: {}",
        err.message
    );
}
#[tokio::test]
async fn idle_timeout_secs_sync_env_var_overrides_default() {
    // 优先级链第 2 级：manifest 未声明时全局环境变量覆盖默认（无效值回退）。
    let _serial = ENV_SENSITIVE_LOCK.lock();
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_sidecar_manifest("env_idle", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);
    let _g = EnvVarGuard::set("AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS", "77");
    assert_eq!(invoker.idle_timeout_secs_sync("env_idle"), 77);
    std::env::set_var("AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS", "not-a-number");
    assert_eq!(
        invoker.idle_timeout_secs_sync("env_idle"),
        agentos_core::traits::default_idle_timeout(),
        "非数字回退默认"
    );
    std::env::set_var("AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS", "0");
    assert_eq!(
        invoker.idle_timeout_secs_sync("env_idle"),
        agentos_core::traits::default_idle_timeout(),
        "0 视为无效回退默认"
    );
}
#[tokio::test]
async fn get_or_create_respawns_stale_solo_host_kills_old_process() {
    // fast path stale 驱逐端到端：指纹缓存回拨 + 记错值 → TTL 过期且指纹必变
    // → kill 旧宿主 + 逐出缓存 → slow path respawn（命令不存在必败，证明走的是
    // spawn 路径而非复用）。
    let tmp = tempfile::tempdir().unwrap();
    std::fs::write(tmp.path().join("server.py"), b"print(1)").unwrap();
    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "stale_solo".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    // entry 非裸 python（不走 venv fail-closed），respawn 时 spawn 必败
    let manifest =
        make_sidecar_manifest("stale_solo", "definitely_missing_command_98765 server.py");
    loader.add_manifest(manifest.clone());
    let invoker = PluginInvokerImpl::new(loader);

    let host_key = solo_host_key("stale_solo");
    let live = spawn_long_lived_stdio_client().await;
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));
    invoker.fingerprints.write().insert(
        host_key.clone(),
        (12345u64, Instant::now() - Duration::from_secs(2)),
    );

    let result = invoker.get_or_create_mcp_client(&manifest).await;
    assert!(
        result.is_err(),
        "stale 驱逐后必须走 respawn（命令不存在必败），不得返回旧实例: {:?}",
        result.map(|_| ()).err()
    );
    wait_until_killed(&live_arc, "stale 驱逐必须 kill 旧宿主进程").await;
    assert!(
        invoker.mcp_clients.read().get(&host_key).is_none(),
        "旧宿主缓存条目必须逐出"
    );
}
#[tokio::test]
async fn unload_broadcasts_on_unload_events_to_bus() {
    // OnUnload 旁路广播两路径：① no-host（InProcess）force_unload 也广播；
    // ② 宿主卸载逐成员广播（合宿组连坐语义的观察面）。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_inprocess_manifest("u_native"));
    loader.add_manifest(make_light_manifest("u_ma", "python server.py"));
    loader.add_manifest(make_light_manifest("u_mb", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);
    let bus = Arc::new(HookEventBus::new(32));
    invoker.set_hook_bus(bus.clone());
    let mut rx = bus.subscribe();

    // ① no-host 路径
    invoker.force_unload_impl("u_native").await.unwrap();
    let ev = tokio::time::timeout(std::time::Duration::from_millis(500), rx.recv())
        .await
        .expect("no-host 路径也要广播 OnUnload")
        .expect("总线未关闭");
    assert_eq!(ev.hook, LifecycleHook::OnUnload);
    assert_eq!(ev.ctx.get("plugin_id"), Some(&json!("u_native")));

    // ② 宿主路径：light 组整组卸载 → 逐成员广播
    invoker.resolve_host_key(&make_light_manifest("u_ma", "python server.py"));
    invoker.resolve_host_key(&make_light_manifest("u_mb", "python server.py"));
    invoker.mcp_clients.write().insert(
        "group:light:1".to_string(),
        Arc::new(tokio::sync::RwLock::new(McpClient::new_http(
            "http://127.0.0.1:9/mcp",
            HashMap::new(),
            None,
        ))),
    );
    invoker.unload_host("group:light:1", true).await.unwrap();
    let mut unloaded = Vec::new();
    for _ in 0..2 {
        let ev = tokio::time::timeout(std::time::Duration::from_millis(500), rx.recv())
            .await
            .expect("宿主卸载必须逐成员广播")
            .expect("总线未关闭");
        assert_eq!(ev.hook, LifecycleHook::OnUnload);
        unloaded.push(
            ev.ctx
                .get("plugin_id")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_string(),
        );
    }
    unloaded.sort();
    assert_eq!(
        unloaded,
        vec!["u_ma".to_string(), "u_mb".to_string()],
        "宿主卸载必须逐成员广播 OnUnload"
    );
}
#[test]
fn existing_host_key_and_members_edge_variants() {
    // 反查与成员集边界：light 声明但外部 MCP → 独占键兜底；独占键成员 = 键内
    // 嵌 plugin_id；无前缀杂键 → 空成员集（不 panic）。
    let loader = Arc::new(MockLoader::new());
    let mut ext = make_light_manifest("ext_guard", "external");
    ext.mcp = Some(McpConfig {
        allowed_roots: Vec::new(),
        transport: McpTransport::StreamableHttp,
        endpoint: Some(McpEndpoint {
            url: Some("http://127.0.0.1:9/mcp".to_string()),
            ..Default::default()
        }),
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: None,
    });
    loader.add_manifest(ext);
    let invoker = PluginInvokerImpl::new(loader);
    assert_eq!(
        invoker.existing_host_key_for("ext_guard"),
        Some("plugin:ext_guard".to_string()),
        "light 声明但外部 MCP 不进合宿 → 独占键"
    );
    assert_eq!(
        invoker.host_members("plugin:ext_guard"),
        vec!["ext_guard".to_string()]
    );
    assert!(
        invoker.host_members("bogus-key").is_empty(),
        "无前缀键 → 空成员集"
    );
}
#[test]
fn resolve_fingerprint_is_zero_without_plugin_dir() {
    // 目录不可得（插件已移除）→ 指纹 0（调用方视为已变化触发 respawn 暴露）。
    let loader = Arc::new(MockLoader::new());
    let manifest = make_sidecar_manifest("ghost_fp", "python server.py");
    let invoker = PluginInvokerImpl::new(loader);
    assert_eq!(
        invoker.resolve_fingerprint(&manifest),
        0,
        "无目录时指纹必须为 0"
    );
}
#[tokio::test]
async fn send_lifecycle_hook_native_with_loader_load_failure_still_ok() {
    // InProcess + 已注入 native loader：send_hook_via_execute 里 load_native
    // 失败（manifest 缺 native 声明）→ Err 仅 warn，钩子调用恒 Ok（不阻断管道）。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_inprocess_manifest("hook_native_load"));
    let invoker =
        PluginInvokerImpl::new(loader).set_native_loader(Arc::new(NativePluginLoader::new()));
    let r = invoker
        .send_lifecycle_hook(
            "hook_native_load",
            LifecycleHook::OnLoad,
            &HookContext::new(),
        )
        .await;
    assert!(r.is_ok(), "native 钩子加载失败只 warn 不阻断: {r:?}");
}
/// 组名合法性边界：空串 / 超长 / 非法字符（冒号会破坏 host_key 结构）
/// 一律拒绝；字母数字与 `_`/`-` 组合放行；长度恰好在 32 字边界内。
#[test]
fn is_valid_group_name_boundaries() {
    assert!(is_valid_group_name("light"));
    assert!(is_valid_group_name("light_stable-2"));
    assert!(!is_valid_group_name(""), "空组名非法");
    assert!(!is_valid_group_name("a:b"), "冒号破坏 host_key 结构");
    assert!(
        !is_valid_group_name("组名"),
        "非 ASCII 非法（进 spawn 参数）"
    );
    assert!(is_valid_group_name(&"x".repeat(32)), "32 字恰在允许内");
    assert!(!is_valid_group_name(&"x".repeat(33)), "33 字超限");
}
/// 宿主键编解码往返：组键 → (组名, 槽位)，非组键与畸形槽位返回 None。
#[test]
fn group_host_key_roundtrip_and_parse_rejects_foreign_keys() {
    let key = group_host_key("light_stable", 7);
    assert_eq!(key, "group:light_stable:7");
    assert_eq!(parse_group_slot(&key), Some(("light_stable", 7)));

    assert_eq!(parse_group_slot("plugin:llm_core"), None, "独占键无组槽");
    assert_eq!(parse_group_slot("group:light"), None, "缺槽位段");
    assert_eq!(
        parse_group_slot("group:light:not-a-number"),
        None,
        "槽位非数字"
    );
    assert_eq!(solo_host_key("llm_core"), "plugin:llm_core");
}
/// 宿主目录向上探测：插件目录层级不固定（生产为拍平一层，历史布局可更深），
/// 逐级向上找最近的含 `_host` 子目录的祖先；无则 None。
#[test]
fn find_group_host_dir_walks_up_to_nearest_ancestor() {
    let tmp = tempfile::tempdir().unwrap();
    // 深层合成成员目录（验逐级上探；生产拍平布局为一层）
    let member = tmp.path().join("shared/demo/member");
    std::fs::create_dir_all(&member).unwrap();
    // 宿主目录在非直接父层
    let host = tmp.path().join("shared/_host");
    std::fs::create_dir_all(&host).unwrap();

    let found = find_group_host_dir(&member).expect("应向上找到 _host");
    assert_eq!(found, host, "取最近祖先的 _host 子目录");

    // 无 _host 的树 → None（保守独占）
    let orphan = tmp.path().join("orphan/member");
    std::fs::create_dir_all(&orphan).unwrap();
    assert_eq!(find_group_host_dir(&orphan), None);
}
/// 合宿成员判定矩阵：组名缺失/非法、host_type 非 Sidecar、外部 MCP
/// （StreamableHttp 带 url / stdio 带 command）各自落回"不参与合宿"。
#[test]
fn is_cohost_member_exclusion_matrix() {
    // 合法组名 + sidecar + 无外部 MCP → 准入
    let ok = make_light_manifest("coh_ok", "python server.py");
    assert!(is_cohost_member(&ok));

    // 组名缺失
    let mut no_group = make_sidecar_manifest("no_group", "python server.py");
    no_group.host_group = None;
    assert!(!is_cohost_member(&no_group));

    // host_type 非 Sidecar（InProcess 天生单进程）
    let mut inproc = make_light_manifest("inproc", "python server.py");
    inproc.host_type = HostType::InProcess;
    assert!(!is_cohost_member(&inproc), "InProcess 不进合宿组");

    // 外部 MCP（StreamableHttp 带 url）：进程归外部所有
    let mut ext_http = make_light_manifest("ext_http", "external");
    ext_http.mcp = Some(McpConfig {
        allowed_roots: Vec::new(),
        transport: McpTransport::StreamableHttp,
        endpoint: Some(McpEndpoint {
            url: Some("http://127.0.0.1:9/mcp".to_string()),
            ..Default::default()
        }),
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: None,
    });
    assert!(!is_cohost_member(&ext_http));

    // 外部 MCP（stdio 带 command）：第三方命令同样不进组
    let mut ext_stdio = make_light_manifest("ext_stdio", "external");
    ext_stdio.mcp = Some(McpConfig {
        allowed_roots: Vec::new(),
        transport: McpTransport::Stdio,
        endpoint: Some(McpEndpoint {
            command: Some("npx".to_string()),
            ..Default::default()
        }),
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: None,
    });
    assert!(!is_cohost_member(&ext_stdio));

    // 带 mcp 配置但 endpoint 为空：无外部进程声明 → 照常参与合宿
    let mut no_endpoint = make_light_manifest("no_endpoint", "python server.py");
    no_endpoint.mcp = Some(McpConfig {
        allowed_roots: Vec::new(),
        transport: McpTransport::StreamableHttp,
        endpoint: None,
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: None,
    });
    assert!(
        is_cohost_member(&no_endpoint),
        "无 endpoint 声明 = 无外部进程，仍参与合宿"
    );
}
#[test]
fn test_sanitize_schema_whitelist_beats_injection() {
    let mut m = make_sidecar_manifest("mcp_x", "mcp:external");
    m.entry = "mcp:external".to_string();
    m.capabilities.tools = vec![agentos_core::traits::ToolCapability {
        name: "Snapshot".to_string(),
        description: None,
        input_schema: Some(json!({
            "type": "object",
            "properties": {"use_vision": {"type": "boolean"}}
        })),
        output_schema: None,
        category: None,
        ui: None,
        render: None,
        isolation_capable: None,
        smoke: None,
        timeout_ms: None,
    }];
    let inputs = json!({
        "use_vision": true,
        "loc": [1, 2],
        "parent_agent_level": 1,
        "pipeline_id": "p1",
        "thread_id": "t1",
        "timestamp": "2026-09-18T00:00:00Z",
        "user_id": 1,
        "_call_context": {"k": 1},
        "session_id": "s1"
    });
    let out = sanitize_external_mcp_arguments(&m, "Snapshot", &inputs);
    // 声明白名单：仅 use_vision 幸存（loc 未声明也剥——注入键会随调用方增长，
    // 枚举是打地鼠；schema 就是白名单）
    assert_eq!(out, json!({"use_vision": true}));
}
#[test]
fn test_sanitize_unknown_schema_falls_back_to_blocklist() {
    let mut m = make_sidecar_manifest("mcp_x", "mcp:external");
    m.entry = "mcp:external".to_string();
    m.capabilities.tools = vec![agentos_core::traits::ToolCapability {
        name: "Mystery".to_string(),
        description: None,
        input_schema: None,
        output_schema: None,
        category: None,
        ui: None,
        render: None,
        isolation_capable: None,
        smoke: None,
        timeout_ms: None,
    }];
    let inputs = json!({"query": "x", "_call_context": {}, "pipeline_id": "p"});
    let out = sanitize_external_mcp_arguments(&m, "Mystery", &inputs);
    assert_eq!(out, json!({"query": "x"}));
}
#[test]
fn test_sanitize_non_object_passthrough() {
    let m = make_sidecar_manifest("mcp_x", "mcp:external");
    assert_eq!(
        sanitize_external_mcp_arguments(&m, "T", &json!(null)),
        json!(null)
    );
    assert_eq!(
        sanitize_external_mcp_arguments(&m, "T", &json!([1, 2])),
        json!([1, 2])
    );
}
#[test]
fn test_sanitize_null_values_dropped_for_declared() {
    let mut m = make_sidecar_manifest("mcp_x", "mcp:external");
    m.entry = "mcp:external".to_string();
    m.capabilities.tools = vec![agentos_core::traits::ToolCapability {
        name: "Click".to_string(),
        description: None,
        input_schema: Some(
            json!({"type": "object", "properties": {"label": {"type": "integer"}, "loc": {"type": "array"}}}),
        ),
        output_schema: None,
        category: None,
        ui: None,
        render: None,
        isolation_capable: None,
        smoke: None,
        timeout_ms: None,
    }];
    let inputs = json!({"label": 3, "loc": null});
    let out = sanitize_external_mcp_arguments(&m, "Click", &inputs);
    assert_eq!(out, json!({"label": 3}));
}
/// R1：成员软卸载后分配条目保留（待重加入标记）。暂离不构成漂移——其余成员
/// 的调用照常复用宿主进程（成员粒度隔离，装机版 2026-10-09 修复面：暂离计入
/// 漂移曾使宿主被任意成员的调用整组 reload）；被卸成员**自己**被调用时才触发
/// 整宿主 respawn，respawn 按分配表重建成员集把被卸成员**一次性**带回进程
/// ——旧实现（分配同摘）下每个被卸成员各自重装箱各自触发一次整杀，装机版
/// 实测 GC 每轮卸 15-20 名成员 → 分钟级重载风暴。
#[tokio::test]
async fn member_soft_unload_keeps_assignment_for_single_rejoin() {
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_light_manifest("a_service", "python server.py"));
    loader.add_manifest(make_light_manifest("b_service", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);
    invoker.assign_light_host_with("a_service", "light", 6);
    invoker.assign_light_host_with("b_service", "light", 6);

    let host_key = "group:light:1".to_string();
    // spawn 后记账：快照 = 全体成员（respawn 重建的基准面）
    invoker.spawned_members.write().insert(
        host_key.clone(),
        vec!["a_service".into(), "b_service".into()],
    );

    invoker.finalize_member_unload(&host_key, "a_service").await;

    // 分配条目保留 = 待重加入标记（R1 核心断言）
    assert!(
        invoker
            .light_packing
            .read()
            .assignments
            .contains_key("a_service"),
        "软卸载必须保留分配条目（待重加入标记）"
    );
    // 进程服务面收缩
    assert_eq!(
        invoker.spawned_members.read().get(&host_key).cloned(),
        Some(vec!["b_service".to_string()]),
        "快照必须摘除被卸成员"
    );
    // 暂离记账：待重加入集登记被卸成员（漂移检测的免疫依据）
    assert!(
        invoker
            .pending_rejoin_members
            .read()
            .get(&host_key)
            .is_some_and(|p| p.contains("a_service")),
        "软卸载必须登记待重加入成员（暂离记账）"
    );
    // 暂离不构成漂移：其余成员的调用照常复用宿主进程；被卸成员自己被调用
    // 才触发重装箱 respawn（k→1，respawn 按分配表一次带回全部暂离成员）
    assert!(
        !invoker.light_host_members_drifted(&host_key, "b_service"),
        "暂离成员的缺席不得构成漂移（其余成员照常复用宿主）"
    );
    assert!(
        invoker.light_host_members_drifted(&host_key, "a_service"),
        "被卸成员自己被调用：进程无其服务面，必须触发重装箱 respawn"
    );
    // respawn 按分配表重建成员集 → 被卸成员随同一次 respawn 回归（k 次 → 1 次）
    assert_eq!(
        invoker.host_members(&host_key),
        vec!["a_service".to_string(), "b_service".to_string()],
        "respawn 成员集必须含待重加入成员"
    );
    // 空闲时钟已清：GC 不会对它二次卸载
    assert!(
        !invoker.member_last_used.read().contains_key("a_service"),
        "被卸成员空闲记账必须清除"
    );
}
/// R1 配套：软/整回收判定按**快照口径**（进程实际服务面）——进程内最后一名
/// 活跃成员空闲超阈必须整组回收。旧表长口径会被待重加入成员虚胖成
/// 「多成员宿主」，走向对空服务面进程的软卸载（宿主协议拒绝 → GC 只 warn，
/// 宿主永不释放）。本用例无存活客户端（崩溃驱逐后形态），整组回收路径
/// 必须照常完成记账清空 + 槽位回收。
#[tokio::test]
async fn idle_gc_whole_host_reclaim_for_last_active_member_with_pending_rejoin_phantom() {
    // 大幅回拨环境前置（见 uptime_supports_backdate）：开机不足 40_000s 跳过
    if !uptime_supports_backdate(40_000) {
        return;
    }
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_light_manifest("a_service", "python server.py"));
    loader.add_manifest(make_light_manifest("b_service", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);
    invoker.assign_light_host_with("a_service", "light", 6);
    invoker.assign_light_host_with("b_service", "light", 6);

    let host_key = "group:light:1".to_string();
    // a_service 已软卸载（分配保留 + 快照收缩 + 空闲时钟清除），宿主进程已死
    // （无缓存客户端）；b_service 空闲超阈。
    invoker
        .spawned_members
        .write()
        .insert(host_key.clone(), vec!["b_service".into()]);
    backdate_idle(&invoker, "b_service", 40_000);

    invoker.run_idle_gc_pass().await;

    assert!(
        !invoker
            .light_packing
            .read()
            .assignments
            .contains_key("a_service"),
        "整组回收必须清掉待重加入成员的分配条目（槽位回收）"
    );
    assert!(
        !invoker
            .light_packing
            .read()
            .assignments
            .contains_key("b_service"),
        "整组回收必须清掉最后活跃成员的分配条目（槽位回收）"
    );
    assert!(
        !invoker.spawned_members.read().contains_key(&host_key),
        "整组回收必须清空 spawn 快照"
    );
}
/// R1 配套：强制卸载一个「分配在、快照无」（软卸载待重加入）的成员——
/// 清分配标记即完成语义，宿主与其余成员不得连坐。
#[tokio::test]
async fn force_unload_pending_rejoin_member_clears_assignment_without_host_kill() {
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_light_manifest("a_service", "python server.py"));
    loader.add_manifest(make_light_manifest("b_service", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);
    invoker.assign_light_host_with("a_service", "light", 6);
    invoker.assign_light_host_with("b_service", "light", 6);

    let host_key = "group:light:1".to_string();
    invoker
        .spawned_members
        .write()
        .insert(host_key.clone(), vec!["b_service".into()]);
    // 占位客户端哨兵：若路径误入整组 kill 会被移除，断言即红
    invoker.mcp_clients.write().insert(
        host_key.clone(),
        Arc::new(tokio::sync::RwLock::new(McpClient::new_stdio(
            "python".to_string(),
            vec![],
        ))),
    );

    invoker
        .force_unload_impl("a_service")
        .await
        .expect("强制卸载待重加入成员应成功");

    assert!(
        !invoker
            .light_packing
            .read()
            .assignments
            .contains_key("a_service"),
        "强制卸载必须撤销待重加入标记（不随下次 respawn 回归）"
    );
    assert!(
        invoker
            .light_packing
            .read()
            .assignments
            .contains_key("b_service"),
        "其余成员分配不受影响"
    );
    assert!(
        invoker.mcp_clients.read().contains_key(&host_key),
        "无进程内服务面可摘：不得整组 kill（客户端哨兵必须存活）"
    );
}
/// R1 配套：unload_group_member 的最后成员判定按快照口径——进程内只剩一名
/// 服务成员（其余为待重加入标记）时必须 Err 回退整组路径，不得向宿主发
/// 空服务面卸载请求（合宿不变量：成员集至少一名）。
#[tokio::test]
async fn unload_group_member_rejects_last_process_member_with_pending_rejoin_phantom() {
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_light_manifest("a_service", "python server.py"));
    loader.add_manifest(make_light_manifest("b_service", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);
    invoker.assign_light_host_with("a_service", "light", 6);
    invoker.assign_light_host_with("b_service", "light", 6);

    let host_key = "group:light:1".to_string();
    invoker
        .spawned_members
        .write()
        .insert(host_key.clone(), vec!["b_service".into()]);

    let err = invoker
        .unload_group_member("b_service")
        .await
        .expect_err("进程内最后一名服务成员必须拒绝成员级卸载");
    assert!(
        err.message.contains("最后成员"),
        "错误文案应点明最后成员语义: {}",
        err.message
    );
}
/// D1 宽限面记账：spawn 窗口内 is_host_starting 必为真，guard drop（成功/
/// 失败/panic 任何退出路径）后必须出清——泄漏一个条目 = 该插件端点宽限常开。
#[tokio::test]
async fn is_host_starting_reflects_spawn_window_bookkeeping() {
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    assert!(
        !invoker.is_host_starting("p1"),
        "无 spawn 窗口时不得报 starting"
    );
    invoker.starting_plugins.write().insert("p1".into());
    assert!(
        invoker.is_host_starting("p1"),
        "spawn 窗口内必须报 starting"
    );
    {
        let _guard = StartingPluginsGuard {
            set: &invoker.starting_plugins,
            members: vec!["p1".into(), "p2".into()],
        };
    }
    assert!(
        !invoker.is_host_starting("p1"),
        "guard drop 后必须出清（RAII 出窗语义）"
    );
}
/// 强制卸载经成员粒度路径成功（真宿主）：unload_group_member 成功后必须撤销
/// 「待重加入」分配标记（R1 保留分配是 idle 语境；强制语义 = 不再自动回归），
/// 快照收缩、宿主进程存活。
#[tokio::test]
async fn force_unload_group_member_via_protocol_revokes_rejoin_marker() {
    let ok = std::process::Command::new(python_exe())
        .arg("-c")
        .arg("import agentos_plugin_sdk, mcp")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    if !ok {
        eprintln!("python 或 agentos_plugin_sdk 不可用，跳过 force_unload 端到端");
        return;
    }

    let tmp = tempfile::tempdir().unwrap();
    for (dirname, pid) in [("a_service", "a_service"), ("b_service", "b_service")] {
        let member_dir = tmp.path().join("tools").join(dirname);
        std::fs::create_dir_all(&member_dir).unwrap();
        std::fs::write(
            member_dir.join("plugin.json"),
            format!(r#"{{"id": "{pid}"}}"#),
        )
        .unwrap();
        std::fs::write(
            member_dir.join("server.py"),
            format!(
                concat!(
                    "from agentos_plugin_sdk import AgentOSPlugin\n",
                    "plugin = AgentOSPlugin(\"{pid}\")\n",
                    "@plugin.tool(name='probe', schema={{'type': 'object'}})\n",
                    "async def probe() -> dict:\n",
                    "    return {{'value': 1}}\n",
                ),
                pid = pid,
            ),
        )
        .unwrap();
    }
    let host_py = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../plugins/shared/_host/host.py");
    let shared_root = tmp.path().to_string_lossy().replace('\\', "/");
    let driver = format!(
        r#"
import sys
sys.path.insert(0, r"{host_py_dir}")
import host
sys.exit(host.main(["--group", "light", "--slot", "1", "--members", "a_service,b_service"],
                   shared_root=__import__("pathlib").Path(r"{shared_root}")))
"#,
        host_py_dir = host_py.parent().unwrap().display(),
        shared_root = shared_root,
    );

    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_light_manifest("a_service", "python server.py"));
    loader.add_manifest(make_light_manifest("b_service", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);
    invoker.assign_light_host_with("a_service", "light", 6);
    invoker.assign_light_host_with("b_service", "light", 6);

    let mut client = McpClient::new_stdio(python_exe().to_string(), vec!["-c".to_string(), driver]);
    client.connect().await.expect("真宿主 spawn 应成功");
    client
        .initialize(&serde_json::json!({}))
        .await
        .expect("握手应成功");
    let host_key = "group:light:1".to_string();
    let shared = Arc::new(tokio::sync::RwLock::new(client));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&shared));
    invoker
        .spawned_members
        .write()
        .insert(host_key.clone(), invoker.host_members(&host_key));

    invoker
        .force_unload_impl("a_service")
        .await
        .expect("成员粒度强制卸载应成功");

    assert!(
        !invoker
            .light_packing
            .read()
            .assignments
            .contains_key("a_service"),
        "强制语义必须撤销待重加入标记（区别于 idle 软卸载的保留）"
    );
    assert!(
        invoker
            .light_packing
            .read()
            .assignments
            .contains_key("b_service"),
        "其余成员分配不受影响"
    );
    assert_eq!(
        invoker.spawned_members.read().get(&host_key).cloned(),
        Some(vec!["b_service".to_string()]),
        "快照必须收缩"
    );
    assert!(shared.read().await.is_alive().await, "宿主进程必须存活");
    let tools = shared.read().await.list_tools().await.expect("tools/list");
    let tools_str = serde_json::to_string(&tools).unwrap();
    assert!(
        !tools_str.contains("a_service.probe"),
        "被卸成员工具退出服务面: {tools_str}"
    );
    assert!(
        tools_str.contains("b_service.probe"),
        "其余成员工具保持可达: {tools_str}"
    );
    invoker.shutdown_all().await;
}
#[tokio::test]
async fn host_boxes_snapshot_empty_invoker_is_empty() {
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    let snap = invoker.host_boxes_snapshot().await;
    assert!(snap.hosts.is_empty());
    assert!(snap.pending_spawn.is_empty());
    // 响应契约顶层形状冻结
    assert_eq!(
        serde_json::to_value(&snap).unwrap(),
        json!({"hosts": [], "pending_spawn": []})
    );
}
/// 组宿主盒子（待重加入成员/启动窗口/宿主与成员在飞/last_call 取成员最大值）
/// + 独占盒子（服务面 = 键内嵌插件、无重加入概念）+ pending_spawn 差集含
/// enabled 两态（loader 已知 / 记账残留）+ serde 序列化字段名冻结。
#[tokio::test]
async fn host_boxes_snapshot_group_and_solo_boxes_with_pending_rejoin() {
    let loader = Arc::new(MockLoader::new());
    // eager_p：记账可见（启动窗口）且 loader 已知 → pending_spawn enabled=true
    loader.add_manifest(make_light_manifest("eager_p", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);

    // 组宿主：进程缓存 + spawn 记账 + spawn 快照（只含 bash_tool → member_x 待重加入）
    invoker
        .mcp_clients
        .write()
        .insert("group:light:1".to_string(), unconnected_stdio_client());
    invoker
        .host_spawned_at
        .write()
        .insert("group:light:1".to_string(), Instant::now());
    invoker
        .spawned_members
        .write()
        .insert("group:light:1".to_string(), vec!["bash_tool".to_string()]);
    // 分配表：两成员同挂组宿主
    invoker
        .light_packing
        .write()
        .assignments
        .insert("bash_tool".to_string(), "group:light:1".to_string());
    invoker
        .light_packing
        .write()
        .assignments
        .insert("member_x".to_string(), "group:light:1".to_string());
    // 独占宿主：仅进程缓存（独占无 spawned_members 记账面）
    invoker
        .mcp_clients
        .write()
        .insert("plugin:solo_a".to_string(), unconnected_stdio_client());

    // 调用时钟：member_x 先、bash_tool 后（成员按字典序遍历，后触达者 epoch
    // 更大 → last_call_at 必须取成员最大值 = bash_tool）
    invoker.touch_member_last_used("member_x");
    tokio::time::sleep(Duration::from_millis(50)).await;
    invoker.touch_member_last_used("bash_tool");
    invoker.touch_member_last_used("ghost");
    invoker
        .starting_plugins
        .write()
        .insert("bash_tool".to_string());
    invoker
        .starting_plugins
        .write()
        .insert("eager_p".to_string());
    invoker
        .inflight_calls
        .write()
        .insert("group:light:1".to_string(), Arc::new(AtomicUsize::new(2)));
    invoker
        .member_inflight_calls
        .write()
        .insert("bash_tool".to_string(), Arc::new(AtomicUsize::new(2)));

    let snap = invoker.host_boxes_snapshot().await;

    // hosts：组宿主 + 独占宿主，按 host_key 排序
    assert_eq!(snap.hosts.len(), 2);
    assert_eq!(snap.hosts[0].host_key, "group:light:1");
    assert_eq!(snap.hosts[1].host_key, "plugin:solo_a");

    let group = &snap.hosts[0];
    assert_eq!(group.kind, HostBoxKind::Group);
    // 未连接 stdio client = 判死（缓存残留语义），无 pid
    assert!(!group.alive);
    assert_eq!(group.pid, None);
    assert_eq!(group.commit_mb, None, "内存归 api 侧富化，invoker 恒 None");
    let uptime = group.uptime_secs.expect("有 spawn 记账应有 uptime");
    assert!(
        uptime < 3600,
        "刚 spawn 的宿主 uptime 应为小值，got {uptime}"
    );
    assert_eq!(group.spawned_members, vec!["bash_tool".to_string()]);
    assert_eq!(group.members.len(), 2);
    let bt = group
        .members
        .iter()
        .find(|m| m.plugin_id == "bash_tool")
        .unwrap();
    assert!(!bt.pending_rejoin, "在 spawn 快照内的成员不待重加入");
    let mx = group
        .members
        .iter()
        .find(|m| m.plugin_id == "member_x")
        .unwrap();
    assert!(mx.pending_rejoin, "在分配表但不在快照 = 待重加入");
    assert_eq!(group.in_flight, 2);
    // 装箱余量观测面：分配表口径挂载数 + 快照时刻上限（≥ 当前环境覆盖值，
    // 与装箱判定同源——全满才开新槽位）
    assert_eq!(group.slot_used, 2, "分配表两成员同挂组宿主");
    assert!(
        group.slot_cap >= 2,
        "组宿主 slot_cap 至少容纳现有挂载，got {}",
        group.slot_cap
    );
    assert_eq!(group.member_in_flight.get("bash_tool"), Some(&2));
    assert_eq!(group.member_in_flight.len(), 1, "零在飞成员不列");
    assert!(group.starting);
    assert_eq!(group.starting_members, vec!["bash_tool".to_string()]);
    let last_call = group.last_call_at.expect("有调用时钟应有 last_call_at");
    let now = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap()
        .as_secs_f64();
    assert!(
        last_call > 0.0 && last_call <= now,
        "last_call_at 应落在 (0, now]，got {last_call}"
    );

    let solo = &snap.hosts[1];
    assert_eq!(solo.kind, HostBoxKind::Solo);
    assert_eq!(
        solo.spawned_members,
        vec!["solo_a".to_string()],
        "独占无快照记账，服务面 = 键内嵌插件"
    );
    assert_eq!(solo.members.len(), 1);
    assert_eq!(
        (solo.slot_used, solo.slot_cap),
        (1, 1),
        "独占宿主结构性单成员，1/1"
    );
    assert!(!solo.members[0].pending_rejoin, "独占成员无重加入概念");
    assert_eq!(solo.in_flight, 0);
    assert!(solo.member_in_flight.is_empty());
    assert!(!solo.starting);
    assert_eq!(solo.last_call_at, None, "从未调用 = null");
    assert_eq!(solo.uptime_secs, None, "无 spawn 记账 = null");

    // pending_spawn：差集 = {eager_p, ghost}，按 plugin_id 排序
    let ids: Vec<&str> = snap
        .pending_spawn
        .iter()
        .map(|p| p.plugin_id.as_str())
        .collect();
    assert_eq!(
        ids,
        vec!["eager_p", "ghost"],
        "bash_tool/member_x 有分配表条目、solo_a 有宿主条目，不入差集"
    );
    let eager = &snap.pending_spawn[0];
    assert!(eager.enabled, "loader 已知（manifest 可解析）= true");
    assert_eq!(eager.last_call_at, None);
    let ghost = &snap.pending_spawn[1];
    assert!(!ghost.enabled, "记账残留（manifest 已不可得）= false");
    assert!(ghost.last_call_at.is_some());

    // serde 序列化形状冻结（前端逐字消费的字段名；集合断言不依赖键序）
    let v = serde_json::to_value(&snap).unwrap();
    let top = v.as_object().unwrap();
    assert_eq!(top.len(), 2);
    assert!(top.contains_key("hosts") && top.contains_key("pending_spawn"));
    let mut box_keys: Vec<&str> = v["hosts"][0]
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    box_keys.sort_unstable();
    assert_eq!(
        box_keys,
        vec![
            "alive",
            "commit_mb",
            "host_key",
            "in_flight",
            "kind",
            "last_call_at",
            "member_in_flight",
            "members",
            "pid",
            "slot_cap",
            "slot_used",
            "spawned_members",
            "starting",
            "starting_members",
            "uptime_secs",
        ]
    );
    assert_eq!(v["hosts"][0]["kind"], "group");
    assert_eq!(v["hosts"][1]["kind"], "solo");
    let mut member_keys: Vec<&str> = v["hosts"][0]["members"][0]
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    member_keys.sort_unstable();
    assert_eq!(member_keys, vec!["pending_rejoin", "plugin_id"]);
    let mut ps_keys: Vec<&str> = v["pending_spawn"][0]
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    ps_keys.sort_unstable();
    assert_eq!(ps_keys, vec!["enabled", "last_call_at", "plugin_id"]);

    // 性质断言（跨盒子不变量，不钉具体夹具值）
    for h in &snap.hosts {
        assert_eq!(h.starting, !h.starting_members.is_empty());
        assert!(
            h.member_in_flight.values().copied().sum::<u64>() <= h.in_flight,
            "成员在飞之和不得超过宿主在飞"
        );
        if h.kind == HostBoxKind::Group {
            for m in &h.members {
                if !m.pending_rejoin {
                    assert!(
                        h.spawned_members.contains(&m.plugin_id),
                        "非待重加入成员必须在 spawn 快照内"
                    );
                }
            }
        }
    }
}
/// 组宿主 spawn 快照缺失（异常态）→ 全员按待重加入渲染 + spawned_members 空
/// （fail-closed 显示，与漂移检测同口径）。
#[tokio::test]
async fn host_boxes_snapshot_group_without_spawn_snapshot_renders_all_pending_rejoin() {
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    invoker
        .mcp_clients
        .write()
        .insert("group:light:1".to_string(), unconnected_stdio_client());
    invoker
        .light_packing
        .write()
        .assignments
        .insert("m1".to_string(), "group:light:1".to_string());
    invoker
        .light_packing
        .write()
        .assignments
        .insert("m2".to_string(), "group:light:1".to_string());

    let snap = invoker.host_boxes_snapshot().await;
    assert_eq!(snap.hosts.len(), 1);
    let g = &snap.hosts[0];
    assert!(g.spawned_members.is_empty(), "组宿主无快照 = 服务面未知");
    assert!(
        g.members.iter().all(|m| m.pending_rejoin),
        "快照缺失按不在处理（fail-closed）"
    );
}
/// group_member_configs：组宿主 → 每成员各自的注入命名空间（无并集无共享）；
/// 独占宿主 → None（params 不携带字段，行为零变化）；manifest 缺失成员跳过。
#[tokio::test]
async fn group_member_configs_maps_each_member_own_namespace() {
    let loader = Arc::new(MockLoader::new());
    // 全量配置树：resolve_config_path 归一化（去 config/ 前缀与 .yaml 后缀）后下钻。
    loader.set_full_config(json!({
        "plugins": {"isolation": {"isolation_config": {
            "providers": {"wsl_native": {"enabled": true}}
        }}}
    }));
    let trigger = make_light_manifest("ctx_build", "python server.py"); // 无 config_files（触发成员同型）
    let guard = manifest_with_config_file(
        "iso_guard",
        "isolation_config",
        "config/plugins/isolation/isolation_config.yaml",
    );
    // ghost_m 只装箱不登记 manifest（构造并发卸载窗口：get_manifest 缺失形态）
    loader.add_manifest(trigger.clone());
    loader.add_manifest(guard.clone());
    let invoker = PluginInvokerImpl::new(loader.clone());
    for pid in ["ctx_build", "iso_guard", "ghost_m"] {
        invoker.assign_light_host_with(pid, "light", 10);
    }

    // 组宿主：每成员条目 = 自己的命名空间（触发成员无声明 = 空对象）。
    let map = invoker
        .group_member_configs("group:light:1")
        .await
        .expect("组宿主必返回映射");
    let map = map.expect("组宿主必 Some");
    assert_eq!(
        map["iso_guard"]["isolation_config"]["providers"]["wsl_native"]["enabled"],
        json!(true),
        "非触发成员必拿到自己的 config_files 命名空间: {map}"
    );
    assert_eq!(
        map["ctx_build"],
        json!({}),
        "无声明成员收空配置（P6：不并集不共享）: {map}"
    );
    assert!(
        map.get("ghost_m").is_none(),
        "manifest 缺失成员跳过（回落顶层 config = 旧行为）: {map}"
    );

    // 独占宿主：None——initialize params 不携带 member_configs 字段。
    loader.add_manifest(make_sidecar_manifest("solo_p", "python server.py"));
    assert!(
        invoker
            .group_member_configs("plugin:solo_p")
            .await
            .expect("独占判定不报错")
            .is_none(),
        "独占宿主不得携带 member_configs（行为零变化）"
    );
}
/// 端到端（真 host.py 合宿宿主 + 真 SDK）：触发成员（无 config_files）spawn
/// 宿主后，非触发成员的构造期配置（on_load 快照，isolation_guard 同型消费）
/// 必须到达该成员自己的命名空间，且触发成员不得看到他人命名空间（P6 隔离）。
#[tokio::test]
#[allow(clippy::await_holding_lock)] // 刻意跨 await：串行锁护住进程级 env 注入全程（组注释见 HOST_START_TIMEOUT_ENV_LOCK）
async fn cohost_handshake_delivers_member_own_config_to_non_trigger_member() {
    let ok = std::process::Command::new(python_exe())
        .arg("-c")
        .arg("import agentos_plugin_sdk, mcp")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    if !ok {
        eprintln!("python 或 agentos_plugin_sdk 不可用，跳过合宿握手注入端到端");
        return;
    }

    let member_server_py = concat!(
        "from agentos_plugin_sdk import AgentOSPlugin\n",
        "plugin = AgentOSPlugin(\"member\")\n",
        "_warm_config: dict = {}\n",
        "@plugin.on_load\n",
        "async def _on_load(params: dict) -> None:\n",
        "    # 构造期快照（isolation_guard 同型消费：on_load 预热单例定型配置）\n",
        "    global _warm_config\n",
        "    _warm_config = dict(plugin.get_config())\n",
        "@plugin.tool(name='dump_warm_config', schema={'type': 'object'})\n",
        "async def dump_warm_config() -> dict:\n",
        "    return dict(_warm_config)\n",
    );
    let tmp = tempfile::tempdir().unwrap();
    for pid in ["ctx_build", "iso_guard"] {
        let dir = tmp.path().join(pid);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("plugin.json"), format!(r#"{{"id": "{pid}"}}"#)).unwrap();
        std::fs::write(dir.join("server.py"), member_server_py).unwrap();
    }
    let host_py = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../plugins/shared/_host/host.py");
    let shared_root = tmp.path().to_string_lossy().replace('\\', "/");
    let driver = format!(
        r#"
import sys
sys.path.insert(0, r"{host_py_dir}")
import host
sys.exit(host.main(["--group", "light", "--slot", "1", "--members", "ctx_build,iso_guard"],
                   shared_root=__import__("pathlib").Path(r"{shared_root}")))
"#,
        host_py_dir = host_py.parent().unwrap().display(),
        shared_root = shared_root,
    );

    let loader = Arc::new(MockLoader::new());
    loader.set_full_config(json!({
        "plugins": {"isolation": {"isolation_config": {
            "providers": {"wsl_native": {"enabled": true}}
        }}}
    }));
    let trigger = make_light_manifest("ctx_build", "python server.py");
    let guard = manifest_with_config_file(
        "iso_guard",
        "isolation_config",
        "config/plugins/isolation/isolation_config.yaml",
    );
    loader.add_manifest(trigger.clone());
    loader.add_manifest(guard.clone());
    let invoker = PluginInvokerImpl::new(loader);
    invoker.assign_light_host_with("ctx_build", "light", 10);
    invoker.assign_light_host_with("iso_guard", "light", 10);

    // 走真实激活链（connect → initialize 握手含 member_configs → on_load 广播
    // → 入缓存）：触发成员 = ctx_build（无 config_files，部署实测同型）。
    // 激活链启动窗是进程级 env（并行组内其他用例会钉 1s 快失败形态）——入组
    // 串行锁并钉足默认窗（120s：真宿主冷启动 ×2 成员 import），guard drop 恢复。
    let _env_serial = HOST_START_TIMEOUT_ENV_LOCK.lock();
    let _start_window = EnvVarGuard::set("AGENTOS_MCP_START_TIMEOUT_SECS", "120");
    let client = McpClient::new_stdio(python_exe().to_string(), vec!["-c".to_string(), driver]);
    invoker
        .connect_initialize_and_cache(client, "group:light:1", &trigger)
        .await
        .expect("合宿宿主激活链应成功");

    let host_client = invoker
        .mcp_clients
        .read()
        .get("group:light:1")
        .cloned()
        .expect("宿主应已入缓存");

    // 非触发成员（iso_guard）：on_load 构造期快照必含自己的命名空间——
    // 这是部署形态下 isolation_guard fail-closed 的精确回归面。
    let guard_view = host_client
        .read()
        .await
        .call_tool("iso_guard.dump_warm_config", &json!({}))
        .await
        .expect("非触发成员工具应可达");
    let guard_view = mcp_text_payload(&guard_view);
    assert_eq!(
        guard_view["isolation_config"]["providers"]["wsl_native"]["enabled"],
        json!(true),
        "非触发成员构造期必拿到自己的 config_files 命名空间: {guard_view}"
    );

    // 触发成员（ctx_build）：快照 = 自己的命名空间（空），不得看到他人命名空间
    // （member_configs 按成员路由，不是并集——P6 注入隔离不放松）。
    let trigger_view = host_client
        .read()
        .await
        .call_tool("ctx_build.dump_warm_config", &json!({}))
        .await
        .expect("触发成员工具应可达");
    let trigger_view = mcp_text_payload(&trigger_view);
    assert_eq!(
        trigger_view,
        json!({}),
        "触发成员不得收到他人 config_files 命名空间: {trigger_view}"
    );

    invoker.shutdown_all().await;
}
/// 三个不同延迟的 responder 经 ensure_hosts_ready 拉起：总时长 ≈ 最慢者
/// （≪串行和）；就绪等待真实发生（≥ 最慢延迟量级）；keep-warm 常驻集不被
/// 触碰（idle GC 治理不变——常驻声明面用户裁定挂起）；全部宿主入缓存。
#[tokio::test]
#[allow(clippy::await_holding_lock)] // 刻意跨 await：串行锁护 spawn 全程不被 env 组测试改写启动窗（同组口径）
async fn ensure_hosts_ready_overlaps_spawn_waits_and_skips_keep_warm() {
    if abs_python_interpreter().is_none() {
        eprintln!("python 不可用或解释器路径含空白，跳过整组并行 respawn 时序用例");
        return;
    }
    // 本用例默认启动窗 120s；env 组测试会把进程级 AGENTOS_MCP_START_TIMEOUT_SECS
    // 设 1s——不持锁并行时 slow_a 的 2s 延迟会被压窗超时（时序假红）。同锁串行。
    let _env_guard = HOST_START_TIMEOUT_ENV_LOCK.lock();
    let tmp = tempfile::tempdir().unwrap();
    std::fs::write(tmp.path().join("plugin.json"), br#"{"id": "x"}"#).unwrap();

    let loader = Arc::new(MockLoader::new());
    let mut ids = Vec::new();
    for (id, delay) in [("slow_a", 2.0), ("slow_b", 1.6), ("slow_c", 1.2)] {
        let entry = write_delayed_responder(tmp.path(), &format!("{id}.py"), delay);
        let m = make_sidecar_manifest(id, &entry);
        loader.add_manifest(m);
        loader
            .plugin_dirs
            .write()
            .insert(id.to_string(), tmp.path().to_string_lossy().into_owned());
        ids.push(id.to_string());
    }
    let invoker = Arc::new(PluginInvokerImpl::new(loader.clone()));

    let started = Instant::now();
    invoker.ensure_hosts_ready(&ids).await;
    let elapsed = started.elapsed();

    // 串行下界 = 2.0+1.6+1.2 = 4.8s 纯延迟；并行后 ≈ max(2.0) + spawn 开销。
    assert!(
        elapsed >= Duration::from_millis(1900),
        "最慢宿主的延迟必须真实流逝（就绪等待成立，实际 {elapsed:?}）"
    );
    assert!(
        elapsed < Duration::from_millis(3600),
        "三宿主必须重叠拉起（实际 {elapsed:?}；串行 ≥4.8s）"
    );
    for id in &ids {
        assert!(
            invoker
                .mcp_clients
                .read()
                .contains_key(&format!("plugin:{id}")),
            "{id} 必须已就绪入缓存"
        );
    }
    assert!(
        invoker.keep_warm_plugins.read().is_empty(),
        "请求路径 respawn 不得登记 keep-warm 常驻集（挂起面不触碰）"
    );
    assert!(
        invoker.starting_plugins.read().is_empty(),
        "就绪后 spawn 窗记账必须出窗即清"
    );
    invoker.shutdown_all().await;
}
/// 失败隔离：组内一个快败宿主（立即退出）不拖死整组——其余宿主照常拉起
/// 入缓存，失败宿主无残留缓存条目；ensure 本身限时完成不悬挂。
#[tokio::test]
async fn ensure_hosts_ready_isolates_single_host_failure() {
    let Some(interpreter) = abs_python_interpreter() else {
        eprintln!("python 不可用或解释器路径含空白，跳过整组失败隔离用例");
        return;
    };
    let tmp = tempfile::tempdir().unwrap();
    std::fs::write(tmp.path().join("plugin.json"), br#"{"id": "x"}"#).unwrap();
    // 快败替身：秒退（无应答无 EOF 悬挂）→ spawn 路径确定性失败。
    std::fs::write(tmp.path().join("boom.py"), "import sys\nsys.exit(3)\n").unwrap();
    let boom_entry = format!("{interpreter} boom.py");

    let loader = Arc::new(MockLoader::new());
    let mut ids = vec!["boom1".to_string()];
    for id in ["ok_a", "ok_b"] {
        let entry = write_delayed_responder(tmp.path(), &format!("{id}.py"), 0.2);
        loader.add_manifest(make_sidecar_manifest(id, &entry));
        loader
            .plugin_dirs
            .write()
            .insert(id.to_string(), tmp.path().to_string_lossy().into_owned());
        ids.push(id.to_string());
    }
    loader.add_manifest(make_sidecar_manifest("boom1", &boom_entry));
    loader.plugin_dirs.write().insert(
        "boom1".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    let invoker = Arc::new(PluginInvokerImpl::new(loader));

    tokio::time::timeout(Duration::from_secs(30), invoker.ensure_hosts_ready(&ids))
        .await
        .expect("ensure 必须限时完成（失败隔离，不得悬挂）");
    assert!(
        invoker.mcp_clients.read().contains_key("plugin:ok_a")
            && invoker.mcp_clients.read().contains_key("plugin:ok_b"),
        "健康宿主必须照常拉起入缓存"
    );
    assert!(
        !invoker.mcp_clients.read().contains_key("plugin:boom1"),
        "失败宿主不得残留缓存条目"
    );
    invoker.shutdown_all().await;
}
/// 混合引用集：缺失 manifest 的 id 与 native（InProcess）id 跳过，仅 sidecar
/// 拉起；重复 ensure 幂等（暖宿主复用，无二次 spawn、无重复缓存条目）。
#[tokio::test]
async fn ensure_hosts_ready_skips_non_sidecar_and_is_idempotent() {
    let Some(_interpreter) = abs_python_interpreter() else {
        eprintln!("python 不可用或解释器路径含空白，跳过混合引用集用例");
        return;
    };
    let tmp = tempfile::tempdir().unwrap();
    std::fs::write(tmp.path().join("plugin.json"), br#"{"id": "x"}"#).unwrap();
    let entry = write_delayed_responder(tmp.path(), "solo.py", 0.2);

    let loader = Arc::new(MockLoader::new());
    let mut native = make_sidecar_manifest("nat", &entry);
    native.host_type = HostType::InProcess;
    loader.add_manifest(native);
    loader.add_manifest(make_sidecar_manifest("solo", &entry));
    loader.plugin_dirs.write().insert(
        "solo".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    let invoker = Arc::new(PluginInvokerImpl::new(loader));

    let ids = vec!["solo".to_string(), "nat".to_string(), "missing".to_string()];
    invoker.ensure_hosts_ready(&ids).await;
    assert_eq!(
        invoker.mcp_clients.read().len(),
        1,
        "仅 sidecar 拉起（native/缺失 id 跳过）"
    );
    invoker.ensure_hosts_ready(&ids).await;
    assert_eq!(
        invoker.mcp_clients.read().len(),
        1,
        "重复 ensure 幂等：暖宿主复用不重复 spawn"
    );
    invoker.shutdown_all().await;
}
#[tokio::test]
#[allow(clippy::await_holding_lock)] // 守卫已显式 drop 后再 await；clippy 数据流不识别 drop 提前释放
async fn ensure_hosts_ready_expands_declared_service_deps() {
    // R7 F3：声明面依赖闭包——引用集内插件的 requires_services 点式条目
    // （ns.method = 精确运行时调用面）解析提供方并一并并行拉起（llm_core 声明
    // llm.complete_stream → llm_service，litellm 导入离开回收后首调关键路径）。
    // boot 预热集（referenced_plugin_ids 单源）不受影响。
    let Some(_interpreter) = abs_python_interpreter() else {
        eprintln!("python 不可用，跳过声明面依赖闭包用例");
        return;
    };
    let tmp = tempfile::tempdir().unwrap();
    std::fs::write(tmp.path().join("plugin.json"), br#"{"id": "x"}"#).unwrap();
    let entry_c = write_delayed_responder(tmp.path(), "consumer.py", 0.2);
    let entry_p = write_delayed_responder(tmp.path(), "provider.py", 0.2);

    let loader = Arc::new(MockLoader::new());
    let mut consumer = make_sidecar_manifest("llm_core", &entry_c);
    consumer.requires_services = vec!["llm.complete_stream".to_string()];
    loader.add_manifest(consumer);
    loader.add_manifest(make_sidecar_manifest("llm_service", &entry_p));
    loader.plugin_dirs.write().insert(
        "llm_core".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    loader.plugin_dirs.write().insert(
        "llm_service".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    loader.service_providers.write().insert(
        "llm.complete_stream".to_string(),
        vec!["llm_service".to_string()],
    );
    let invoker = Arc::new(PluginInvokerImpl::new(loader));

    invoker.ensure_hosts_ready(&["llm_core".to_string()]).await;

    let clients = invoker.mcp_clients.read();
    assert!(
        clients.contains_key("plugin:llm_core") && clients.contains_key("plugin:llm_service"),
        "声明面点式依赖的提供方必须随引用方一并拉起，实际 {:?}",
        clients.keys().collect::<Vec<_>>()
    );
    drop(clients);
    invoker.shutdown_all().await;
}
#[tokio::test]
#[allow(clippy::await_holding_lock)] // 守卫已显式 drop 后再 await；clippy 数据流不识别 drop 提前释放
async fn ensure_hosts_ready_does_not_expand_bare_namespace_deps() {
    // 防拟合对照：bare 命名空间条目（全量 packaging 样板声明）不展开——注册闸
    // 存在性语义，调用面保持纯懒加载（否则 python_packager 被拖进每次 run）。
    let Some(_interpreter) = abs_python_interpreter() else {
        eprintln!("python 不可用，跳过 bare 命名空间对照用例");
        return;
    };
    let tmp = tempfile::tempdir().unwrap();
    std::fs::write(tmp.path().join("plugin.json"), br#"{"id": "x"}"#).unwrap();
    let entry_c = write_delayed_responder(tmp.path(), "consumer.py", 0.2);
    let entry_p = write_delayed_responder(tmp.path(), "provider.py", 0.2);

    let loader = Arc::new(MockLoader::new());
    let mut consumer = make_sidecar_manifest("llm_core", &entry_c);
    consumer.requires_services = vec!["llm".to_string()];
    loader.add_manifest(consumer);
    loader.add_manifest(make_sidecar_manifest("llm_service", &entry_p));
    loader.plugin_dirs.write().insert(
        "llm_core".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    loader.plugin_dirs.write().insert(
        "llm_service".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    loader
        .service_providers
        .write()
        .insert("llm".to_string(), vec!["llm_service".to_string()]);
    let invoker = Arc::new(PluginInvokerImpl::new(loader));

    invoker.ensure_hosts_ready(&["llm_core".to_string()]).await;

    let clients = invoker.mcp_clients.read();
    assert_eq!(
        clients.keys().collect::<Vec<_>>(),
        vec!["plugin:llm_core"],
        "bare 命名空间依赖不得展开（提供方保持纯懒加载）"
    );
    drop(clients);
    invoker.shutdown_all().await;
}
#[tokio::test]
async fn native_pre_dispatch_executes_tool_and_builds_entry() {
    let calls = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let invoker = PluginInvokerImpl::new(std::sync::Arc::new(MockLoader::new()));
    invoker.set_router(std::sync::Arc::new(PreDispatchRouter {
        calls: calls.clone(),
        delay_ms: 0,
        fail: false,
        envelope_fail: false,
    }));
    let state = json!({
        "current_call": {"name": "bash_execute", "id": "c1", "args": {"command": "ls"}},
        "session_id": "s1",
        "pipeline_id": "p1",
        "message_id": "m1",
        "workspace": "D:\\ws",
    });
    let ctx = make_e2e_ctx(&state);
    let entry = invoker
        .pre_dispatch_native("pipeline_tool_core", &pre_dispatch_manifest(), &ctx)
        .await
        .expect("pre-dispatch entry");
    assert_eq!(entry["success"], true);
    assert_eq!(entry["tool_name"], "bash_execute");
    assert_eq!(entry["call_id"], "c1");
    assert_eq!(entry["data"], json!({"output": "ok"}));

    let recorded = calls.lock().unwrap().clone();
    assert_eq!(recorded.len(), 1, "工具执行只发生一次");
    assert_eq!(recorded[0]["tool_name"], "bash_execute");
    assert_eq!(recorded[0]["args"], json!({"command": "ls"}));
    assert_eq!(recorded[0]["workspace"], "D:\\ws");
    assert_eq!(
        recorded[0]["_call_context"],
        json!({"call_id": "c1", "pipeline_id": "p1", "message_id": "m1", "thread_id": "s1"})
    );
}
#[tokio::test]
async fn native_pre_dispatch_skips_when_guard_pre_decided() {
    let calls = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let invoker = PluginInvokerImpl::new(std::sync::Arc::new(MockLoader::new()));
    invoker.set_router(std::sync::Arc::new(PreDispatchRouter {
        calls: calls.clone(),
        delay_ms: 0,
        fail: false,
        envelope_fail: false,
    }));
    let state = json!({
        "current_call": {"name": "bash_execute", "id": "c1", "args": {}},
        "pre_decided_results": [
            {"call_id": "c1", "tool_name": "bash_execute", "success": false, "error": "blocked"}
        ],
    });
    let ctx = make_e2e_ctx(&state);
    assert!(
        invoker
            .pre_dispatch_native("pipeline_tool_core", &pre_dispatch_manifest(), &ctx)
            .await
            .is_none(),
        "guard 已拦截的调用不得执行"
    );
    assert!(calls.lock().unwrap().is_empty());
}
#[tokio::test]
async fn native_pre_dispatch_failure_and_timeout_become_failed_entries() {
    let calls = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    // M4 派生界：目标工具 "f" 经注册表反查到 tiny_tool（工具级 timeout_ms: 30
    // 声明）——预派发等待界按声明派生为 30ms（旧 `.with_pre_dispatch_timeout`
    // 测试注入口随常量退役删除）。
    let loader = std::sync::Arc::new(MockLoader::new());
    loader.add_manifest({
        let mut m = make_sidecar_manifest("tiny_tool", "python server.py");
        m.capabilities.tools = vec![p12_tool_cap("f", Some(30))];
        m
    });
    let registry = std::sync::Arc::new(agentos_plugin_loader::CapabilityRegistryImpl::new());
    registry.register_tool(
        "tiny_tool",
        agentos_core::traits::ToolDescriptor {
            name: "f".to_string(),
            description: String::new(),
            plugin_id: "tiny_tool".to_string(),
            input_schema: json!({}),
            output_schema: None,
            category: agentos_core::types::ToolCategory::System,
            source: agentos_core::types::ToolSource::Mcp,
            ui: None,
            render: None,
            isolation_capable: None,
        },
    );
    let invoker = PluginInvokerImpl::new(loader).with_tool_registry(registry);
    invoker.set_router(std::sync::Arc::new(PreDispatchRouter {
        calls: calls.clone(),
        delay_ms: 0,
        fail: true,
        envelope_fail: false,
    }));
    let state = json!({"current_call": {"name": "f", "id": "c1", "args": {}}});
    let ctx = make_e2e_ctx(&state);
    let entry = invoker
        .pre_dispatch_native("pipeline_tool_core", &pre_dispatch_manifest(), &ctx)
        .await
        .expect("failed entry");
    assert_eq!(entry["success"], false);
    assert!(entry["error"].as_str().unwrap().contains("dispatch boom"));

    invoker.set_router(std::sync::Arc::new(PreDispatchRouter {
        calls,
        delay_ms: 200,
        fail: false,
        envelope_fail: false,
    }));
    let ctx = make_e2e_ctx(&state);
    let entry = invoker
        .pre_dispatch_native("pipeline_tool_core", &pre_dispatch_manifest(), &ctx)
        .await
        .expect("timeout entry");
    assert_eq!(entry["success"], false);
    assert!(entry["error"].as_str().unwrap().contains("timed out"));
}
/// 工具自身报失败（信封 success:false，如 fs 位置闸拒绝）时，日志必须如实
/// 记"工具内错误"，不得写"native tool pre-dispatch failed"把根因引向内核
/// 派发面（2026-10-17 事故调查被该措辞误导实证）。
#[tokio::test]
async fn native_pre_dispatch_tool_reported_failure_log_is_truthful() {
    let messages = std::sync::Arc::new(parking_lot::Mutex::new(Vec::new()));
    let invoker = PluginInvokerImpl::new(std::sync::Arc::new(MockLoader::new()));
    invoker.set_router(std::sync::Arc::new(PreDispatchRouter {
        calls: std::sync::Arc::new(std::sync::Mutex::new(Vec::new())),
        delay_ms: 0,
        fail: false,
        envelope_fail: true,
    }));
    let state = json!({"current_call": {"name": "file_read", "args": {}}});
    let ctx = make_e2e_ctx(&state);
    let guard = tracing::subscriber::set_default(MessageCapture {
        messages: messages.clone(),
    });
    let entry = invoker
        .pre_dispatch_native("pipeline_tool_core", &pre_dispatch_manifest(), &ctx)
        .await
        .expect("envelope entry");
    assert_eq!(entry["success"], false);
    drop(guard);

    let captured = messages.lock().clone();
    assert!(
        !captured.is_empty(),
        "工具报失败必须落一条 warn（可观测性）"
    );
    assert!(
        captured
            .iter()
            .any(|m| m.contains("tool-internal") && m.contains("tool reported failure")),
        "工具内失败日志须如实标注 tool-internal: {captured:?}"
    );
    assert!(
        !captured.iter().any(|m| m.contains("pre-dispatch failed")),
        "工具内失败不得再记成内核预派发失败: {captured:?}"
    );
}
#[tokio::test]
async fn tool_admission_limit_bounds_concurrent_tool_calls() {
    let invoker = std::sync::Arc::new(
        PluginInvokerImpl::new(std::sync::Arc::new(MockLoader::new())).with_tool_admission_limit(2),
    );
    assert_eq!(invoker.tool_admission_limit(), 2);
    let p1 = invoker.acquire_tool_slot("p", "t").await.unwrap();
    let p2 = invoker.acquire_tool_slot("p", "t").await.unwrap();
    let invoker3 = std::sync::Arc::clone(&invoker);
    let (acquired_tx, acquired_rx) = tokio::sync::oneshot::channel::<()>();
    let waiter = tokio::spawn(async move {
        let _permit = invoker3.acquire_tool_slot("p", "t").await.unwrap();
        let _ = acquired_tx.send(());
        // 持许可直到测试收尾 abort（许可随 future drop 归还）。
        tokio::time::sleep(Duration::from_secs(5)).await;
    });
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert!(!waiter.is_finished(), "第三个许可应排队等待，不占线程");
    drop(p1);
    tokio::time::timeout(Duration::from_secs(1), acquired_rx)
        .await
        .expect("释放许可后排队者应立刻取得")
        .unwrap();
    drop(p2);
    waiter.abort();
}
/// 锚 a：含 user 的 PluginContext 走 native pre-dispatch → resume_pipeline
/// 派发不再被「调用链未携带用户身份」拒绝，scope 三元组全量透传。
#[tokio::test]
async fn pre_dispatch_scope_carries_user_and_resume_guard_passes() {
    let seen: SeenTenantCalls = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let state = json!({
        "current_call": { "name": "task_manage", "id": "c1", "args": {} },
        "session_id": "s1",
        "pipeline_id": "p1",
    });
    let ctx = ctx_with_user(&state);
    let entry = run_pre_dispatch_with_guard(RESUME_PIPELINE_GUARD_TEXT, &seen, &ctx).await;
    assert_eq!(
        entry["success"], true,
        "resume_pipeline 派发必须通过守卫（A1 断点）：{entry}"
    );
    let got = seen.lock().unwrap().clone();
    assert_eq!(got.len(), 1);
    let (tenant, _params) = &got[0];
    let tenant = tenant.as_ref().expect("预派发链必须带租户 scope");
    assert_eq!(
        tenant.user_id.as_deref(),
        Some("u-maint"),
        "user 必须随派发链 scope 透传"
    );
    assert_eq!(tenant.tenant_id, "t1");
    assert_eq!(tenant.session_id, "s1");
}
/// 锚 b：session.create 在同一条预派发链路通过。
#[tokio::test]
async fn pre_dispatch_scope_carries_user_and_session_create_guard_passes() {
    let seen: SeenTenantCalls = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let state = json!({
        "current_call": { "name": "pipeline_tool", "id": "c2", "args": {} },
        "session_id": "s1",
        "pipeline_id": "p1",
    });
    let ctx = ctx_with_user(&state);
    let entry = run_pre_dispatch_with_guard(SESSION_CREATE_GUARD_TEXT, &seen, &ctx).await;
    assert_eq!(
        entry["success"], true,
        "session.create 必须通过守卫（A1 断点）：{entry}"
    );
    let got = seen.lock().unwrap().clone();
    assert_eq!(got.len(), 1);
    let (tenant, _params) = &got[0];
    assert_eq!(
        tenant
            .as_ref()
            .expect("预派发链必须带租户 scope")
            .user_id
            .as_deref(),
        Some("u-maint"),
        "user 必须随派发链 scope 透传"
    );
}
/// 守卫敏感性对照：父 scope 无 user（两参 `new` 的丢 user 形态）→ 逐字
/// fail-fast 文案拒绝。锁定替身守卫的区分度；修复后语义不变（无 user 可
/// 继承时仍须拒绝，禁静默放行）。
#[tokio::test]
async fn pre_dispatch_without_user_parent_still_rejected_by_guards() {
    let state = json!({
        "current_call": { "name": "task_manage", "id": "c3", "args": {} },
        "session_id": "s1",
        "pipeline_id": "p1",
    });
    let tenant = TenantContext::new("t1", "s1");
    let ctx = PluginContext::new(
        &state,
        json!({}),
        tenant,
        Uuid::new_v4(),
        agentos_core::types::ContentLoader::new(
            std::sync::Arc::new(MockStorage),
            "run1".to_string(),
            "main".to_string(),
        ),
    );
    for guard_text in [RESUME_PIPELINE_GUARD_TEXT, SESSION_CREATE_GUARD_TEXT] {
        let seen: SeenTenantCalls = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let entry = run_pre_dispatch_with_guard(guard_text, &seen, &ctx).await;
        assert_eq!(entry["success"], false, "无 user 父必须被守卫拒绝: {entry}");
        let error = entry["error"].as_str().unwrap_or_default();
        assert!(
            error.contains("调用链未携带用户身份"),
            "拒绝文案必须是身份缺失 fail-fast（锚）：{entry}"
        );
    }
}
