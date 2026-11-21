// @feature: FP-0.2.一 插件协议 | @ci: rust-test
//! 调用基础与恢复（invoke/crash/lifecycle/recovery/spawn backoff/handshake）。

use super::*;
#[test]
fn pre_dispatch_wait_derives_from_target_tool_declaration() {
    // M4（ADR 2026-11-05-timeout-taxonomy-and-merge）：预派发等待界 = 派发
    // 目标工具的声明界（BUG-60 续作（2026-10-19）的「预派发界对齐审批族统一
    // 24h」人肉常量退役——派生天然正确）。三向：声明者（human 形态
    // mcp.request_timeout_secs=86400）→ 86400；沉默者 → 内核 capability 缺省
    // 300s；反查不可达（注册表 miss）→ 缺省 300s。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest({
        let mut m = make_sidecar_manifest("human_tool", "python server.py");
        m.mcp = Some(
            serde_json::from_value(json!({
                "transport": "stdio",
                "request_timeout_secs": 86_400
            }))
            .expect("human 形态 mcp 配置解析"),
        );
        m
    });
    loader.add_manifest(make_sidecar_manifest("silent_tool", "python server.py"));
    let registry = Arc::new(agentos_plugin_loader::CapabilityRegistryImpl::new());
    let descriptor = |name: &str, plugin_id: &str| agentos_core::traits::ToolDescriptor {
        name: name.to_string(),
        description: String::new(),
        plugin_id: plugin_id.to_string(),
        input_schema: json!({}),
        output_schema: None,
        category: agentos_core::types::ToolCategory::System,
        source: agentos_core::types::ToolSource::Mcp,
        ui: None,
        render: None,
        isolation_capable: None,
    };
    registry.register_tool("human_tool", descriptor("human_interaction", "human_tool"));
    registry.register_tool("silent_tool", descriptor("silent_tool", "silent_tool"));

    let invoker = PluginInvokerImpl::new(loader).with_tool_registry(registry);
    assert_eq!(
        invoker.pre_dispatch_wait_ms("tool-executor", "invoke", "human_interaction", None),
        86_400_000,
        "声明者按声明界派生（human 24h 形态）"
    );
    assert_eq!(
        invoker.pre_dispatch_wait_ms("tool-executor", "invoke", "silent_tool", None),
        agentos_core::traits::default_capability_timeout_ms(),
        "沉默者落内核 capability 缺省界"
    );
    assert_eq!(
        invoker.pre_dispatch_wait_ms("tool-executor", "invoke", "ghost_tool", None),
        agentos_core::traits::default_capability_timeout_ms(),
        "注册表 miss 落缺省界"
    );
    assert_eq!(
        invoker.pre_dispatch_wait_ms("event-bus", "emit", "human_interaction", None),
        agentos_core::traits::default_capability_timeout_ms(),
        "非 tool-executor capability 不做工具派生"
    );
}
#[test]
fn host_job_registry_follows_client_record_lifecycle() {
    // ADR 2026-10-19 D4 第二刀登记面：Job 守卫随宿主客户端记录同生命周期
    // （登记 → 随记录释放 → 幂等 → 全量 drain）。守卫用零句柄测试例，不碰
    // 内核对象；kill-on-close 行为面归 host_job 模块真实进程测试。
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    invoker.register_host_job("plugin:x", host_job::test_guard());
    assert!(
        invoker.host_jobs.read().contains_key("plugin:x"),
        "spawn 成功登记后守卫应在册"
    );
    // respawn 覆盖写：同键再登记不叠加（旧守卫被覆盖关闭）
    invoker.register_host_job("plugin:x", host_job::test_guard());
    assert_eq!(invoker.host_jobs.read().len(), 1, "同宿主键覆盖写");
    // 驱逐随记录释放；无条目释放幂等 no-op
    invoker.release_host_job("plugin:x");
    assert!(
        !invoker.host_jobs.read().contains_key("plugin:x"),
        "随记录释放后守卫应出册"
    );
    invoker.release_host_job("plugin:x");
    assert_eq!(invoker.host_jobs.read().len(), 0, "幂等 no-op");
    // 全量 drain（停机/全量废弃路径）
    invoker.register_host_job("plugin:a", host_job::test_guard());
    invoker.register_host_job("plugin:b", host_job::test_guard());
    invoker.clear_host_jobs();
    assert!(invoker.host_jobs.read().is_empty(), "全量 drain 清空");
}
#[test]
fn test_parse_entry_simple() {
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);
    let (cmd, args) = invoker.parse_entry("python3 server.py").unwrap();
    assert_eq!(cmd, "python3");
    assert_eq!(args, vec!["server.py"]);
}
#[test]
fn test_parse_entry_with_args() {
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);
    let (cmd, args) = invoker
        .parse_entry("python3 -m my_plugin --port 8080")
        .unwrap();
    assert_eq!(cmd, "python3");
    assert_eq!(args, vec!["-m", "my_plugin", "--port", "8080"]);
}
#[test]
fn test_parse_entry_empty() {
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);
    assert!(invoker.parse_entry("").is_err());
}
#[tokio::test]
async fn test_invoke_inprocess_without_loader_errors() {
    // InProcess 插件路径已接通 NativePluginLoader：未注入 loader 时应返回
    // NATIVE_LOADER_NOT_CONFIGURED（而非旧的 INPROCESS_DIRECT_CALL 硬错误）。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_inprocess_manifest("rust_plugin"));

    let invoker = PluginInvokerImpl::new(loader);
    let state = json!({});
    let ctx = PluginContext::new(
        &state,
        json!({}),
        TenantContext::new("t1", "s1"),
        Uuid::new_v4(),
        agentos_core::types::ContentLoader::new(
            std::sync::Arc::new(MockStorage),
            "run1".to_string(),
            "main".to_string(),
        ),
    );

    let result = invoker.invoke_pipeline_plugin("rust_plugin", &ctx).await;
    assert!(result.is_err());
    let err = result.unwrap_err();
    assert_eq!(err.code.as_deref(), Some("NATIVE_LOADER_NOT_CONFIGURED"));
}
#[tokio::test]
#[ignore = "native_test 与 tool_core 共用 NativePluginModule_Ref 全局缓存，同进程并行会冲突；tool_core 已由 e2e_native_plugins 覆盖。单独跑：cargo test e2e_native_inprocess -- --ignored"]
// 测试串行化锁需覆盖整个 async 测试体，刻意跨 await 持有，改异步锁会改变串行语义。
#[allow(clippy::await_holding_lock)]
async fn e2e_native_inprocess_plugin_executes() {
    // 单独验证 native_test echo 插件（基础 native 直接 trait 对象链路）。
    // 与 e2e_native_plugins 分离：避免同进程两个同 RootModule 类型插件互相覆盖。
    let _guard = NATIVE_E2E_LOCK.lock();
    let dll = repo_root().join("plugins/shared/native_test/native_test_plugin.dll");
    if !dll.exists() {
        eprintln!("SKIP: native cdylib not built at {}", dll.display());
        return;
    }
    let invoker = fully_wired_invoker_for_e2e().await;
    let state = json!({});
    let ctx = make_e2e_ctx(&state);
    let result = invoker.invoke_pipeline_plugin("native_test", &ctx).await;
    assert!(
        result.is_ok(),
        "native plugin invoke failed: {:?}",
        result.err()
    );
    let pr = result.unwrap();
    assert_eq!(
        pr.state_updates.get("processed_by"),
        Some(&json!("test_plugin")),
        "got: {:?}",
        pr.state_updates
    );
}
#[tokio::test]
async fn test_invoke_nonexistent_plugin() {
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);
    let state = json!({});
    let ctx = PluginContext::new(
        &state,
        json!({}),
        TenantContext::new("t1", "s1"),
        Uuid::new_v4(),
        agentos_core::types::ContentLoader::new(
            std::sync::Arc::new(MockStorage),
            "run1".to_string(),
            "main".to_string(),
        ),
    );

    let result = invoker.invoke_pipeline_plugin("nonexistent", &ctx).await;
    assert!(result.is_err());
}
#[tokio::test]
async fn test_crash_callback_invoked() {
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);

    let crashed = Arc::new(std::sync::Mutex::new(None::<String>));
    let crashed_clone = Arc::clone(&crashed);
    invoker.on_crash(Arc::new(move |plugin_id: &str| {
        *crashed_clone.lock().unwrap() = Some(plugin_id.to_string());
    }));

    invoker.notify_crash("test_plugin");

    assert_eq!(*crashed.lock().unwrap(), Some("test_plugin".to_string()));
}
#[tokio::test]
async fn test_lifecycle_hook_composite_skipped() {
    // ADR ⑥: 组合插件不需要生命周期钩子
    let loader = Arc::new(MockLoader::new());
    let manifest = PluginManifest {
        restricted_capabilities: Vec::new(),
        force_include_tools: Vec::new(),
        state: None,
        id: "composite_test".to_string(),
        name: "Composite".to_string(),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::Composite,
        language: "yaml".to_string(),
        host_type: HostType::InProcess,
        host_group: None,
        entry: String::new(),
        capabilities: Default::default(),
        requires_services: vec![],
        permissions: Default::default(),
        priority: 100,
        mcp: None,
        lifecycle: None,
        native: None,
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        enabled: None,
        activation: None,
        provides: None,
        visibility: Default::default(),
        persistent_fields: vec![],
        aux_venvs: Vec::new(),
        export_fields: vec![],
    };
    loader.add_manifest(manifest);

    let invoker = PluginInvokerImpl::new(loader);
    let ctx = HookContext::new();
    let result = invoker
        .send_lifecycle_hook("composite_test", LifecycleHook::OnLoad, &ctx)
        .await;
    assert!(result.is_ok()); // 组合插件直接返回 Ok
}
#[tokio::test]
async fn test_force_unload_nonexistent() {
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);
    // force_unload 对不存在的插件也应该返回 Ok
    let result = invoker.force_unload("nonexistent").await;
    assert!(result.is_ok());
}
#[tokio::test]
async fn test_recovery_retry_once_then_success() {
    // 第一次尝试死亡（PLUGIN_CRASHED）→ force_unload + respawn → 第二次成功：
    // 调用方拿到 Ok（完全透明），且不触发崩溃回调（插件实际可用）。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_sidecar_manifest("recover_ok", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);

    let crashed = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
    let crashed_clone = Arc::clone(&crashed);
    invoker.on_crash(Arc::new(move |plugin_id: &str| {
        crashed_clone.lock().unwrap().push(plugin_id.to_string());
    }));

    let attempts = Arc::new(std::sync::Mutex::new(0u32));
    let attempts_clone = Arc::clone(&attempts);
    let result: Result<serde_json::Value, PluginError> = invoker
        .with_transparent_recovery("recover_ok", || async {
            let n = {
                let mut c = attempts_clone.lock().unwrap();
                *c += 1;
                *c
            };
            if n == 1 {
                Err(PluginError {
                    message: "plugin process died mid-call".to_string(),
                    code: Some("PLUGIN_CRASHED".to_string()),
                    source: Some("plugin-invoker".to_string()),
                })
            } else {
                Ok(serde_json::json!({"recovered": true}))
            }
        })
        .await;

    assert_eq!(*attempts.lock().unwrap(), 2, "死亡后必须恰好重试一次");
    assert_eq!(result.unwrap()["recovered"], true, "重试成功对调用方透明");
    assert!(
        crashed.lock().unwrap().is_empty(),
        "透明恢复成功不应触发崩溃回调"
    );
}
#[tokio::test]
async fn test_recovery_retry_once_only_returns_original_error() {
    // 重试仍失败 → 返回第一次的**原错误**（仅一次重试，防循环），并触发
    // 崩溃回调（恢复失败保留崩溃语义：卸载能力 + last_crash_ts）。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_sidecar_manifest("recover_fail", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);

    let crashed = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
    let crashed_clone = Arc::clone(&crashed);
    invoker.on_crash(Arc::new(move |plugin_id: &str| {
        crashed_clone.lock().unwrap().push(plugin_id.to_string());
    }));

    let attempts = Arc::new(std::sync::Mutex::new(0u32));
    let attempts_clone = Arc::clone(&attempts);
    let result: Result<serde_json::Value, PluginError> = invoker
        .with_transparent_recovery("recover_fail", || async {
            let n = {
                let mut c = attempts_clone.lock().unwrap();
                *c += 1;
                *c
            };
            Err(PluginError {
                message: format!("death #{n}"),
                code: Some("PLUGIN_CRASHED".to_string()),
                source: Some("plugin-invoker".to_string()),
            })
        })
        .await;

    assert_eq!(*attempts.lock().unwrap(), 2, "仅重试一次，不得循环");
    let err = result.unwrap_err();
    assert_eq!(err.code.as_deref(), Some("PLUGIN_CRASHED"));
    assert_eq!(err.message, "death #1", "重试失败返回第一次的原错误");
    assert_eq!(
        crashed.lock().unwrap().as_slice(),
        &["recover_fail".to_string()],
        "恢复失败必须触发一次崩溃回调"
    );
}
#[tokio::test]
async fn test_recovery_non_death_error_no_retry() {
    // 非 death 类失败（MCP_CALL_FAILED 等）不重试——协议/工具错误 respawn 无益。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_sidecar_manifest("no_retry", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);

    let attempts = Arc::new(std::sync::Mutex::new(0u32));
    let attempts_clone = Arc::clone(&attempts);
    let result: Result<serde_json::Value, PluginError> = invoker
        .with_transparent_recovery("no_retry", || async {
            *(attempts_clone.lock().unwrap()) += 1;
            Err(PluginError {
                message: "MCP call failed: protocol error".to_string(),
                code: Some("MCP_CALL_FAILED".to_string()),
                source: Some("plugin-invoker".to_string()),
            })
        })
        .await;

    assert_eq!(*attempts.lock().unwrap(), 1, "非 death 错误不重试");
    assert_eq!(result.unwrap_err().code.as_deref(), Some("MCP_CALL_FAILED"));
}
#[test]
fn test_is_recoverable_sidecar_death_classification() {
    // 死亡分类：PLUGIN_CRASHED 可透明恢复；stdio 运输写失败（MCP_CALL_FAILED
    // 携带 transport error + write/flush/broken pipe 特征）视同死亡可恢复
    // （写失败只可能发生在已死进程句柄上，与 is_dead_sidecar 写失败标记互补）。
    let mk = |code: &str, msg: &str| PluginError {
        message: msg.to_string(),
        code: Some(code.to_string()),
        source: None,
    };
    assert!(PluginInvokerImpl::is_recoverable_sidecar_death(&mk(
        "PLUGIN_CRASHED",
        "x"
    )));
    // 运输写失败 → 可恢复（Windows os error 232 本地化消息同样命中）
    assert!(PluginInvokerImpl::is_recoverable_sidecar_death(&mk(
        "MCP_CALL_FAILED",
        "MCP call failed: tool call failed: isolation_guard.execute: transport error: write newline error: 管道正在被关闭。 (os error 232)"
    )));
    assert!(PluginInvokerImpl::is_recoverable_sidecar_death(&mk(
        "MCP_CALL_FAILED",
        "transport error: flush error: broken pipe"
    )));
    // 非运输失败不恢复：协议错误 / 纯工具错误 / HTTP 网络错误（http post）
    assert!(!PluginInvokerImpl::is_recoverable_sidecar_death(&mk(
        "MCP_CALL_FAILED",
        "MCP call failed: protocol error"
    )));
    assert!(!PluginInvokerImpl::is_recoverable_sidecar_death(&mk(
        "MCP_CALL_FAILED",
        "transport error: http post: connect failed"
    )));
    assert!(!PluginInvokerImpl::is_recoverable_sidecar_death(&mk(
        "MCP_TOOL_CALL_FAILED",
        "transport error: write error: broken pipe"
    )));
    assert!(!PluginInvokerImpl::is_recoverable_sidecar_death(&mk(
        "MCP_CONNECT_FAILED",
        "x"
    )));
    assert!(!PluginInvokerImpl::is_recoverable_sidecar_death(
        &PluginError {
            message: "x".to_string(),
            code: None,
            source: None,
        }
    ));
}
#[test]
fn test_is_dead_sidecar_http_client_never_dead() {
    // HTTP transport 无子进程（pid=None）→ 永不判死（is_alive 恒 false 的坑）。
    // 用未连接的 HTTP 客户端模拟（child=None，与 HTTP 连接后同构——
    // connect 的 HTTP 分支不设置 child）。
    let rt = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    let client = McpClient::new_http(
        "http://127.0.0.1:1/mcp",
        std::collections::HashMap::new(),
        None,
    );
    let dead = rt.block_on(async { PluginInvokerImpl::is_dead_sidecar(&client).await });
    assert!(!dead, "HTTP transport（无子进程）不得判为死亡");
}
#[tokio::test]
async fn test_get_or_create_http_cached_client_not_misjudged_dead() {
    // §3.2：HTTP transport 客户端（pid 恒 None、is_alive 恒
    // false——无子进程）进入缓存后，get_or_create_mcp_client 的 fast path
    // 必须判「存活」：返回同一缓存实例，不触发 notify_crash、不逐出重建。
    // 旧实现裸用 is_alive() 判死，HTTP 插件每次调用都误报 "Plugin process
    // crashed" + 崩溃回调 + 缓存逐出重建（对远程 server 无谓重连）。
    // 红测：修复前本用例失败（缓存被逐出、回调被触发、走重建路径）。
    let loader = Arc::new(MockLoader::new());
    let manifest = make_http_manifest("http_cached", "http://127.0.0.1:9/mcp");
    loader.add_manifest(manifest.clone());

    let invoker = PluginInvokerImpl::new(loader);

    let crashed = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
    let crashed_clone = Arc::clone(&crashed);
    invoker.on_crash(Arc::new(move |plugin_id: &str| {
        crashed_clone.lock().unwrap().push(plugin_id.to_string());
    }));

    // 手工把 HTTP 客户端放进缓存（new_http 未连接态与连接后同构：
    // child 恒 None → pid 恒 None，判定只看 pid 门控）。
    // 缓存键 = 宿主键（外部 MCP 不进合宿组 → 独占键 plugin:{id}）。
    let cached: Arc<tokio::sync::RwLock<McpClient>> = Arc::new(tokio::sync::RwLock::new(
        McpClient::new_http("http://127.0.0.1:9/mcp", HashMap::new(), None),
    ));
    invoker
        .mcp_clients
        .write()
        .insert("plugin:http_cached".to_string(), Arc::clone(&cached));

    let got = invoker
        .get_or_create_mcp_client(&manifest)
        .await
        .expect("HTTP transport 缓存客户端不得被误判死亡");

    // 返回的是同一缓存实例（未重建）
    assert!(
        Arc::ptr_eq(&got, &cached),
        "fast path 必须返回缓存实例，不得逐出重建"
    );
    // 缓存条目未被移除/替换
    let in_cache = invoker
        .mcp_clients
        .read()
        .get("plugin:http_cached")
        .cloned();
    assert!(
        in_cache.map(|c| Arc::ptr_eq(&c, &cached)).unwrap_or(false),
        "缓存条目不得被移除"
    );
    // 未触发崩溃回调
    assert!(
        crashed.lock().unwrap().is_empty(),
        "HTTP transport 误判死会触发 notify_crash"
    );
}
#[tokio::test]
async fn test_get_or_create_dead_stdio_sidecar_still_detected_and_rebuilt() {
    // §3.2 回归护栏：stdio sidecar 真死（pid=Some 且进程
    // 已退出）仍必须判死——notify_crash + 缓存逐出 + 走 respawn 路径。
    // fast path 接入 is_dead_sidecar 门控后该既有语义不得回归。
    //
    // 用「spawn 后立即退出」的进程模拟真死（Windows: cmd /c exit 0；
    // Unix: /bin/sh -c exit 0）。
    #[cfg(windows)]
    let (cmd, args): (&str, Vec<&str>) = ("cmd", vec!["/c", "exit", "0"]);
    #[cfg(unix)]
    let (cmd, args): (&str, Vec<&str>) = ("/bin/sh", vec!["-c", "exit", "0"]);
    let entry = format!("{} {}", cmd, args.join(" "));

    let loader = Arc::new(MockLoader::new());
    let manifest = make_sidecar_manifest("stdio_dead", &entry);
    loader.add_manifest(manifest.clone());

    let invoker = PluginInvokerImpl::new(loader);

    let crashed = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
    let crashed_clone = Arc::clone(&crashed);
    invoker.on_crash(Arc::new(move |plugin_id: &str| {
        crashed_clone.lock().unwrap().push(plugin_id.to_string());
    }));

    // 手工构造「子进程已退出」的 stdio 客户端：connect 真实 spawn 后等它自然
    // 退出。注意：等待期间不得对该客户端调 is_alive/pid（tokio Child 被
    // try_wait 观察到退出后 fuse 成 Done，此后 id()=None）——纯 sleep 等
    // 退出，与生产「首次判定前无人 poll 过 child」的时序一致。
    let mut dying = McpClient::new_stdio(cmd, args.iter().map(|s| s.to_string()).collect());
    dying.connect().await.expect("spawn 速死进程应成功");
    let cached: Arc<tokio::sync::RwLock<McpClient>> = Arc::new(tokio::sync::RwLock::new(dying));
    invoker
        .mcp_clients
        .write()
        .insert("plugin:stdio_dead".to_string(), Arc::clone(&cached));

    // 条件等待子进程真死（llvm-cov 插桩下 spawn+exit 变慢，固定等待会假失败）
    wait_until_dead(&cached).await;

    let result = invoker.get_or_create_mcp_client(&manifest).await;

    // 真死被检测：崩溃回调恰好触发一次（含 plugin_id）
    assert_eq!(
        crashed.lock().unwrap().as_slice(),
        &["stdio_dead".to_string()],
        "stdio 真死必须触发 notify_crash（一次）"
    );
    // 死实例被从缓存逐出
    let in_cache = invoker.mcp_clients.read().get("plugin:stdio_dead").cloned();
    assert!(
        !in_cache.map(|c| Arc::ptr_eq(&c, &cached)).unwrap_or(false),
        "死 sidecar 必须从缓存逐出"
    );
    // 走了 respawn 路径：速死命令完不成 initialize 握手 → Err
    // （而非把死实例当存活返回 Ok）。
    assert!(
        result.is_err(),
        "respawn 后 initialize 必然失败（速死命令），不得返回死实例"
    );
}
#[tokio::test]
async fn spawn_backoff_gates_repeat_failures_until_fingerprint_changes() {
    let dir = tempfile::tempdir().unwrap();
    let (invoker, manifest) = breaker_test_invoker(&dir, 3);
    // 卡3 接线：熔断跳闸回调——跨线恰好一次，字段可对账。
    let notices = Arc::new(parking_lot::Mutex::new(Vec::new()));
    let sink = notices.clone();
    invoker.on_spawn_breaker(Arc::new(move |n: &SpawnBreakerNotice| {
        sink.lock().push((
            n.host_key.clone(),
            n.plugin_id.clone(),
            n.members.clone(),
            n.consecutive,
            n.last_error.clone(),
        ));
    }));
    let err_code = |e: PluginError| e.code.unwrap_or_default();

    // 首败：原始错误如实透出（PYPROJECT_MISSING），记账 1。
    let e1 = invoker
        .get_or_create_mcp_client(&manifest)
        .await
        .err()
        .expect("无 pyproject 必败");
    assert_eq!(err_code(e1), "PYPROJECT_MISSING");

    // 立即重试：1s 退避窗内 fail-fast（HOST_SPAWN_BACKOFF），不再打原始错误。
    let e2 = invoker
        .get_or_create_mcp_client(&manifest)
        .await
        .err()
        .expect("退避窗内直接拒绝");
    assert_eq!(err_code(e2), "HOST_SPAWN_BACKOFF");

    // 指纹变化（用户修复）→ 清账放行，错误回到原始根因。
    std::fs::write(dir.path().join("fixed_impl.py"), b"v2").unwrap();
    let e3 = invoker
        .get_or_create_mcp_client(&manifest)
        .await
        .err()
        .expect("修复不彻底仍败");
    assert_eq!(err_code(e3), "PYPROJECT_MISSING", "放行后透出原始根因");

    // 退避窗过后连败至阈值：第 3 败跨线，此后熔断 fail-fast。
    tokio::time::sleep(Duration::from_millis(1100)).await;
    let e4 = invoker
        .get_or_create_mcp_client(&manifest)
        .await
        .err()
        .expect("退避窗过仍败（记账 2）");
    assert_eq!(err_code(e4), "PYPROJECT_MISSING");
    tokio::time::sleep(Duration::from_millis(2100)).await;
    let e5 = invoker
        .get_or_create_mcp_client(&manifest)
        .await
        .err()
        .expect("退避窗过仍败（记账 3，跨线）");
    assert_eq!(err_code(e5), "PYPROJECT_MISSING");
    let e6 = invoker
        .get_or_create_mcp_client(&manifest)
        .await
        .err()
        .expect("熔断 fail-fast");
    assert_eq!(err_code(e6), "HOST_SPAWN_BREAKER_OPEN");

    // 再修 → 指纹变化 → 熔断解除，回到原始根因（恢复通道不设限）。
    std::fs::write(dir.path().join("fixed_impl.py"), b"v3").unwrap();
    let e7 = invoker
        .get_or_create_mcp_client(&manifest)
        .await
        .err()
        .expect("熔断解除后仍按原始根因失败");
    assert_eq!(err_code(e7), "PYPROJECT_MISSING");

    // 跳闸通知恰好一次（记录 3/6/7 三次失败，只有第 3 次跨线）。
    let got = notices.lock().clone();
    assert_eq!(got.len(), 1, "跳闸回调必须恰好一次: {got:?}");
    let (host_key, plugin_id, members, consecutive, last_error) = &got[0];
    assert_eq!(host_key, "plugin:loop_plugin");
    assert_eq!(plugin_id, "loop_plugin");
    assert_eq!(members.as_slice(), &["loop_plugin".to_string()]);
    assert_eq!(*consecutive, 3, "连击数 = 阈值");
    assert!(
        last_error.contains("pyproject.toml"),
        "末次根因透传: {last_error}"
    );
}
/// 预算闸拒绝：结构化错误信封（HOST_SPAWN_BUDGET_REJECTED，原因透传），且
/// 拒绝**不入** spawn_backoff 记账。行为证明不需要探针回调：退避闸先于预算
/// 闸——若预算拒绝曾被记账，第 3 次起会被退避/熔断 fail-fast
/// （HOST_SPAWN_BACKOFF/BREAKER_OPEN）拦截，不再到达预算闸；恒预算信封 +
/// 闸解除后直达原始根因（而非 BREAKER_OPEN）即完整证明。熔断跳闸通知只在
/// record_failure 跨线时产出，预算拒绝路径不经过它，天然零跳闸。
#[tokio::test]
async fn pre_spawn_budget_gate_rejects_without_breaker_accounting() {
    let dir = tempfile::tempdir().unwrap();
    let (invoker, manifest) = breaker_test_invoker(&dir, 3);

    // 闸恒拒：连拒 5 次（> 熔断阈值 3），每次都是预算信封（若退避账本被
    // 污染，此处自第 3 次起改为退避/熔断错误）。
    let reject: PreSpawnGate = Arc::new(|| Err("预算超限（测试注入）".to_string()));
    invoker.set_pre_spawn_gate(reject);
    for _ in 0..5 {
        let e = invoker
            .get_or_create_mcp_client(&manifest)
            .await
            .err()
            .expect("闸拒必须 Err");
        assert_eq!(e.code.as_deref(), Some("HOST_SPAWN_BUDGET_REJECTED"));
        assert!(e.message.contains("预算超限"));
    }

    // 闸解除（换放行闸）：直达原始根因 PYPROJECT_MISSING 而非
    // HOST_SPAWN_BREAKER_OPEN——放行直通 spawn 路径，且前 5 次预算拒绝从未
    // 污染退避账本。
    let allow: PreSpawnGate = Arc::new(|| Ok(()));
    invoker.set_pre_spawn_gate(allow);
    let e = invoker
        .get_or_create_mcp_client(&manifest)
        .await
        .err()
        .expect("无 pyproject 必败");
    assert_eq!(e.code.as_deref(), Some("PYPROJECT_MISSING"));
}
/// 未注入闸（默认 None，预算未启用）= 直通：错误来自原始 spawn 根因而非
/// 预算面（零行为差异回归锚）。
#[tokio::test]
async fn pre_spawn_budget_gate_absent_passes_through() {
    let dir = tempfile::tempdir().unwrap();
    let (invoker, manifest) = breaker_test_invoker(&dir, 3);
    let e = invoker
        .get_or_create_mcp_client(&manifest)
        .await
        .err()
        .expect("无 pyproject 必败");
    assert_eq!(
        e.code.as_deref(),
        Some("PYPROJECT_MISSING"),
        "无闸 = 零开销直通（错误来自原始 spawn 路径）"
    );
}
/// stderr `[cohost][member-offline]` 结构性行 → 注册的观察者（卡3 通知链
/// 端到端）：真 spawn 一个打印该行后存活的替身进程，connect 后观察者收到
/// 解析好的成员下线记录；日志转发照旧（tracing 面无断言）。
#[tokio::test]
async fn cohost_member_offline_stderr_line_reaches_registered_observer() {
    let Some(interpreter) = abs_python_interpreter() else {
        eprintln!("python 不可用，跳过成员下线 stderr 观察用例");
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let script = dir.path().join("host_stub.py");
    std::fs::write(
        &script,
        concat!(
            "import sys, time\n",
            "print('[cohost][member-offline] group=light slot=0 plugin=task_submit ",
            "stage=load error=ImportError: cannot import name X', flush=True, file=sys.stderr)\n",
            "time.sleep(15)\n",
        ),
    )
    .unwrap();
    let received = Arc::new(parking_lot::Mutex::new(Vec::new()));
    let sink = received.clone();
    let mut client = McpClient::new_stdio(&interpreter, vec!["host_stub.py".to_string()])
        .with_working_dir(dir.path())
        .with_stderr_notice_observer(Arc::new(move |n: &MemberOfflineNotice| {
            sink.lock().push(n.clone());
        }));
    client.connect().await.expect("替身宿主应可 spawn");

    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(10);
    while received.lock().is_empty() {
        assert!(
            tokio::time::Instant::now() < deadline,
            "10s 内观察者未收到成员下线记录"
        );
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    let n = received.lock()[0].clone();
    assert_eq!(n.group, "light");
    assert_eq!(n.slot, "0");
    assert_eq!(n.plugin, "task_submit");
    assert_eq!(n.stage, "load");
    assert!(
        n.error.contains("cannot import name X"),
        "error 段全文透传: {}",
        n.error
    );
    client.kill().await.ok();
}
#[test]
fn host_start_timeout_env_parsing_invalid_falls_back_to_default() {
    // 配置注入四组区分度输入：未设置/合法值/非法值（非数字、≤0）——非法一律
    // 回退默认 120s，合法值生效；不 panic、不产生 0 窗（0 窗 = 永远超时）。
    let _env_guard = HOST_START_TIMEOUT_ENV_LOCK.lock();

    // 未设置 → 默认（先取值再恢复环境，断言失败不污染并行测试）
    let original = std::env::var("AGENTOS_MCP_START_TIMEOUT_SECS").ok();
    std::env::remove_var("AGENTOS_MCP_START_TIMEOUT_SECS");
    let unset_value = host_start_timeout();
    if let Some(v) = original {
        std::env::set_var("AGENTOS_MCP_START_TIMEOUT_SECS", v);
    }
    assert_eq!(
        unset_value,
        Duration::from_secs(120),
        "未设置必须回退默认 120s"
    );

    let _env = EnvVarGuard::set("AGENTOS_MCP_START_TIMEOUT_SECS", "45");
    assert_eq!(
        host_start_timeout(),
        Duration::from_secs(45),
        "合法值必须生效"
    );
    drop(_env);

    let _zero = EnvVarGuard::set("AGENTOS_MCP_START_TIMEOUT_SECS", "0");
    assert_eq!(
        host_start_timeout(),
        Duration::from_secs(120),
        "≤0 必须回退默认（0 窗 = 永远超时）"
    );
    drop(_zero);

    let _junk = EnvVarGuard::set("AGENTOS_MCP_START_TIMEOUT_SECS", "abc");
    assert_eq!(
        host_start_timeout(),
        Duration::from_secs(120),
        "非数字必须回退默认"
    );
}
#[test]
fn get_or_create_silent_handshake_fails_fast_with_host_start_timeout() {
    // 启动窗独立时间界（ADR 2026-09-28）的 fail-fast 契约验证。
    //
    // 进程级 env 注入（AGENTOS_MCP_START_TIMEOUT_SECS=1）会泄漏给并行测试——
    // 全部真 spawn 邻居（mock HTTP 宿主族）在注入窗内握手即假红
    // HOST_START_TIMEOUT（llvm-cov 满批实测两例：tool_ok/tool_rpc_err，
    // 本地握手在插桩+满批线程饥饿下超 1s）。故真 spawn 用例隔离到子进程：
    // 同二进制重入（--exact + --ignored worker），env 只注入子进程，母进程
    // 环境全程零触碰——现有与未来的 spawn 邻居一并不再暴露。
    let exe = std::env::current_exe().expect("测试二进制路径可解析");
    let worker = "invoker::tests::invocation::silent_handshake_fails_fast_worker";
    let out = std::process::Command::new(exe)
        .args(["--exact", worker, "--ignored", "--nocapture"])
        .env("AGENTOS_MCP_START_TIMEOUT_SECS", "1")
        .output()
        .expect("子进程测试重入可执行");
    assert!(
        out.status.success(),
        "worker 未通过（env 注入只在子进程生效，母进程不受影响）:\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    // --exact 无匹配时 lib test 恒 exit 0（0 passed）——静默假绿防线
    assert!(
        String::from_utf8_lossy(&out.stdout).contains("1 passed"),
        "worker 必须实际执行（1 passed）——0 passed = 过滤名漂移（静默假绿）"
    );
}
#[tokio::test]
#[ignore = "由 get_or_create_silent_handshake_fails_fast_with_host_start_timeout 以子进程驱动（env 注入只落子进程，不泄漏并行邻居）"]
async fn silent_handshake_fails_fast_worker() {
    // 启动窗独立时间界（ADR 2026-09-28）：存活但永不应答 initialize 的假
    // sidecar 必须在注入的短窗（1s）内 fail-fast 返回 HOST_START_TIMEOUT——
    // 而非挂在 request_timeout（默认 86400s）上钉死 per-host spawn 锁。
    // 超时后 starting_plugins RAII 出窗、spawn 锁释放（后续调用可立即重试
    // respawn，再有界返回不悬挂）。
    //
    // 僵尸形态（A0 复现 §九.1）：sidecar 读走 initialize 请求但不应答也不退出
    // ——stdout 保持打开（无 EOF 快败）、无任何应答 → 挂在等应答。
    //
    // 本用例恒以独立子进程运行（父测注入 env）——进程专属，env 注入就地
    // set_var 无需恢复（无并行邻居可污染）。
    std::env::set_var("AGENTOS_MCP_START_TIMEOUT_SECS", "1");
    let window = 1u64;
    let Some(interpreter) = abs_python_interpreter() else {
        eprintln!("python 不可用或解释器路径含空白，跳过挂死超时用例");
        return;
    };

    let tmp = tempfile::tempdir().unwrap();
    std::fs::write(tmp.path().join("plugin.json"), br#"{"id": "start_to"}"#).unwrap();
    std::fs::write(
        tmp.path().join("silent_server.py"),
        concat!(
            "import sys, time\n",
            "sys.stdin.readline()\n", // 读走 initialize 请求，不应答
            "time.sleep(30)\n",       // 存活不退出（stdout 无 EOF）
        ),
    )
    .unwrap();
    let entry = format!("{interpreter} silent_server.py");

    let loader = Arc::new(MockLoader::new());
    let manifest = make_sidecar_manifest("start_to", &entry);
    loader.add_manifest(manifest.clone());
    loader.plugin_dirs.write().insert(
        "start_to".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    let invoker = Arc::new(PluginInvokerImpl::new(loader));

    // 挂在握手窗内时 starting_plugins 必须在集（D1 宽限面记账先行）
    let m2 = manifest.clone();
    let invoker2 = Arc::clone(&invoker);
    let task = tokio::spawn(async move { invoker2.get_or_create_mcp_client(&m2).await });
    let started = Instant::now();
    let deadline = Instant::now() + Duration::from_secs(10);
    let result = loop {
        if invoker.starting_plugins.read().contains("start_to") {
            // 在窗记账已观察到；等任务走完超时路径
            break task.await.expect("spawn 任务不得 panic");
        }
        if task.is_finished() {
            // 任务已结束却从未观察到在窗记账——把真实结果带进断言，定位前置断裂点
            let r = task.await.expect("spawn 任务不得 panic");
            panic!(
                "任务已结束仍未观察到 spawn 窗记账（starting_plugins 轮询全失）——结果 = {:?}",
                r.as_ref()
                    .err()
                    .map(|e| (e.code.clone(), e.message.clone()))
            );
        }
        assert!(
            Instant::now() < deadline,
            "10s 内未进入 spawn 窗记账（前置不成立）"
        );
        tokio::time::sleep(Duration::from_millis(20)).await;
    };
    let elapsed = started.elapsed();

    let err = match result {
        Err(e) => e,
        Ok(_) => panic!("永不应答 initialize 的 sidecar 必须超时，不得成功"),
    };
    assert_eq!(
        err.code.as_deref(),
        Some("HOST_START_TIMEOUT"),
        "必须返回可辨识 code（区别于 MCP_INIT_FAILED/MCP_CONNECT_FAILED）：{err}"
    );
    // 性质断言：≥ 注入窗长（窗真实生效）、≪ request_timeout 默认（fail-fast）
    assert!(
        elapsed >= Duration::from_secs(window),
        "耗时必须 ≥ 注入窗长（实际 {elapsed:?}）"
    );
    assert!(
        elapsed < Duration::from_secs(20),
        "必须 fail-fast（实际 {elapsed:?}；对照 request_timeout 默认 86400s）"
    );

    // RAII 出窗：starting_plugins 必已清空（超时 drop future → guard drop）
    assert!(
        invoker.starting_plugins.read().is_empty(),
        "超时路径 starting_plugins 必须出窗即清（泄漏 = 端点宽限常开）"
    );
    // 半开客户端未入缓存
    assert!(
        invoker.mcp_clients.read().get("plugin:start_to").is_none(),
        "超时客户端不得入缓存"
    );

    // spawn 锁已释放：后续调用立即返回。卡2 起首次 spawn 失败即入 1s 退避窗
    // （gate 在持锁后判定——快速 HOST_SPAWN_BACKOFF 恰证明锁可进可出，未挂死）；
    // 指纹变化（用户修复）清账后立即真重试 respawn，同样 fail-fast（锁释放 +
    // 恢复通道双证明；若锁被持锁者钉住，本调用将无限悬挂）。
    let started2 = Instant::now();
    let result2 = invoker.get_or_create_mcp_client(&manifest).await;
    let elapsed2 = started2.elapsed();
    assert_eq!(
        result2.err().and_then(|e| e.code).as_deref(),
        Some("HOST_SPAWN_BACKOFF"),
        "首败后立即重试须命中退避窗（fail-fast，锁已释放）"
    );
    assert!(
        elapsed2 < Duration::from_secs(20),
        "第二次调用必须有界返回（实际 {elapsed2:?}）"
    );
    // 指纹 touch：追加注释行改 mtime（内容语义不变——挂死脚本仍挂死，
    // HOST_START_TIMEOUT 分支保持可复现）
    {
        use std::io::Write as _;
        let mut f = std::fs::OpenOptions::new()
            .append(true)
            .open(tmp.path().join("silent_server.py"))
            .unwrap();
        f.write_all(b"\n# touched\n").unwrap();
    }
    let started3 = Instant::now();
    let result3 = invoker.get_or_create_mcp_client(&manifest).await;
    let elapsed3 = started3.elapsed();
    assert_eq!(
        result3.err().and_then(|e| e.code).as_deref(),
        Some("HOST_START_TIMEOUT"),
        "指纹变化清账后必须真重试 respawn 并同样 fail-fast"
    );
    assert!(
        elapsed3 < Duration::from_secs(20),
        "清账后的重试必须有界返回（实际 {elapsed3:?}）"
    );
}
#[tokio::test]
#[allow(clippy::await_holding_lock)] // 刻意跨 await：串行锁护住进程级 env 注入全程（组注释见 HOST_START_TIMEOUT_ENV_LOCK）
async fn get_or_create_fast_death_sidecar_keeps_own_error_not_masked_as_start_timeout() {
    // 区分度输入（符号相反）：速死 sidecar（spawn 后立即退出 → stdout EOF
    // 快败）走既有失败路径，code 保持 MCP_INIT_FAILED——启动窗超时包裹不得
    // 把既有快败分支误标为 HOST_START_TIMEOUT（可辨识码要求逐分支保真）。
    let _env_guard = HOST_START_TIMEOUT_ENV_LOCK.lock();
    let _env = EnvVarGuard::set("AGENTOS_MCP_START_TIMEOUT_SECS", "1");

    #[cfg(windows)]
    let entry = "cmd /c exit 0";
    #[cfg(unix)]
    let entry = "/bin/sh -c exit 0";

    let loader = Arc::new(MockLoader::new());
    let manifest = make_sidecar_manifest("fast_death", entry);
    loader.add_manifest(manifest.clone());
    let invoker = PluginInvokerImpl::new(loader);

    let err = match invoker.get_or_create_mcp_client(&manifest).await {
        Err(e) => e,
        Ok(_) => panic!("速死命令完不成 initialize 握手，必须失败"),
    };
    assert_eq!(
        err.code.as_deref(),
        Some("MCP_INIT_FAILED"),
        "速死快败不得被启动窗超时误标：{err}"
    );
    assert!(
        invoker.starting_plugins.read().is_empty(),
        "快败路径同样 RAII 出窗即清"
    );
}
#[tokio::test]
#[allow(clippy::await_holding_lock)] // 刻意跨 await：串行锁护住进程级 env 注入全程（组注释见 HOST_START_TIMEOUT_ENV_LOCK）
async fn get_or_create_responsive_sidecar_completes_handshake_within_short_window() {
    // 正常路径不受影响：能应答 initialize 的真 sidecar 在同样短的启动窗内
    // 照常握手成功入缓存——超时包裹只分叉挂死路径，成功路径语义逐字节不变。
    // （python 不可用 / 解释器路径含空白时响亮跳过——entry 按空白切分，
    //  带分隔符的显式解释器路径刻意绕过 venv 单轨，见 resolve_sidecar_command。）
    let _env_guard = HOST_START_TIMEOUT_ENV_LOCK.lock();
    let Some(interpreter) = abs_python_interpreter() else {
        eprintln!("python 不可用或解释器路径含空白，跳过正常握手回归");
        return;
    };
    let _env = EnvVarGuard::set("AGENTOS_MCP_START_TIMEOUT_SECS", "10");

    // stdlib 最小 MCP responder：应答 initialize（回显 id）→ 消费 initialized
    // 通知 → 存活待调用（无 SDK 依赖，真 stdio 协议帧）。
    let tmp = tempfile::tempdir().unwrap();
    std::fs::write(tmp.path().join("plugin.json"), br#"{"id": "resp_ok"}"#).unwrap();
    std::fs::write(
        tmp.path().join("server.py"),
        concat!(
            "import sys, json, time\n",
            "line = sys.stdin.readline()\n",
            "req = json.loads(line)\n",
            "resp = {'jsonrpc': '2.0', 'id': req['id'], 'result': {'protocolVersion': '2024-11-05'}}\n",
            "sys.stdout.write(json.dumps(resp) + '\\n')\n",
            "sys.stdout.flush()\n",
            "sys.stdin.readline()\n",
            "time.sleep(30)\n",
        ),
    )
    .unwrap();

    let loader = Arc::new(MockLoader::new());
    let manifest = make_sidecar_manifest("resp_ok", &format!("{interpreter} server.py"));
    loader.add_manifest(manifest.clone());
    loader.plugin_dirs.write().insert(
        "resp_ok".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    let invoker = PluginInvokerImpl::new(loader);

    let started = Instant::now();
    let result = invoker.get_or_create_mcp_client(&manifest).await;
    assert!(
        result.is_ok(),
        "可应答 sidecar 必须在短窗内握手成功（正常路径不受影响）：{:?}",
        result.err()
    );
    assert!(
        started.elapsed() < Duration::from_secs(10),
        "正常握手必须远短于注入窗（实际 {:?}）",
        started.elapsed()
    );
    assert!(
        invoker.mcp_clients.read().get("plugin:resp_ok").is_some(),
        "成功客户端必须按宿主键入缓存"
    );
    assert!(
        invoker.starting_plugins.read().is_empty(),
        "成功路径同样出窗即清"
    );
    invoker.shutdown_all().await;
}
#[tokio::test]
async fn test_shutdown_all_drains_and_kills_cached_sidecars() {
    // §3.3a：内核停机口——mcp_clients 必须**全量 drain**
    // 且每个缓存 sidecar 进程被真实 kill（Ctrl-C / exit 75 后零孤儿）。
    // 覆盖两类有区分度的缓存条目：
    // - stdio sidecar（有子进程）：kill 后进程必须死（性质断言，防假实现只清缓存）；
    // - HTTP 客户端（无子进程）：kill 是 no-op，drain 不炸（HTTP transport 混存常态）。
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));

    let live = spawn_long_lived_stdio_client().await;
    assert!(live.is_alive().await, "前置：长驻假 sidecar 必须存活");
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert("stdio_live".to_string(), Arc::clone(&live_arc));
    invoker.mcp_clients.write().insert(
        "http_cached".to_string(),
        Arc::new(tokio::sync::RwLock::new(McpClient::new_http(
            "http://127.0.0.1:9/mcp",
            HashMap::new(),
            None,
        ))),
    );
    assert_eq!(invoker.mcp_clients.read().len(), 2, "前置：缓存 2 条");

    invoker.shutdown_all().await;

    assert!(
        invoker.mcp_clients.read().is_empty(),
        "shutdown_all 后缓存必须清空（全量 drain）"
    );
    wait_until_killed(&live_arc, "stdio sidecar 进程必须被 kill（不得留孤儿）").await;
}
#[tokio::test]
async fn test_shutdown_all_noop_when_cache_empty() {
    // §3.3a 边界：空缓存 no-op 不抛（boot 后从未调用任何插件就停机）。
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    invoker.shutdown_all().await;
    assert!(invoker.mcp_clients.read().is_empty());
}
#[tokio::test]
async fn test_kill_sidecar_if_any_kills_cached_and_noop_when_absent() {
    // §3.3b：disable 窄口——
    // - 有缓存条目：kill 进程 + 移除缓存（reenable 后按调用懒 spawn 重生）；
    // - 无缓存条目（从未 spawn / HTTP 无子进程）：no-op 不抛。
    // 两组输入一次覆盖（有/无是同一行为的有区分度两分支）。
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));

    let live = spawn_long_lived_stdio_client().await;
    assert!(live.is_alive().await, "前置：长驻假 sidecar 必须存活");
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    // 缓存键 = 宿主键（独占：plugin:{id}；victim 无 manifest，走独占兜底键）
    invoker
        .mcp_clients
        .write()
        .insert("plugin:victim".to_string(), Arc::clone(&live_arc));

    // 无缓存：no-op（不 panic、不影响其他条目）
    invoker.kill_sidecar_if_any("never_spawned").await;
    assert!(
        invoker.mcp_clients.read().get("plugin:victim").is_some(),
        "非目标插件缓存不得被误删"
    );

    // 有缓存：kill + 移除
    invoker.kill_sidecar_if_any("victim").await;
    assert!(
        invoker.mcp_clients.read().get("plugin:victim").is_none(),
        "目标插件缓存必须移除"
    );
    wait_until_killed(&live_arc, "目标插件 sidecar 进程必须被 kill").await;

    // 幂等：再次调用同一 id（已无缓存）仍 no-op 不抛
    invoker.kill_sidecar_if_any("victim").await;
}
