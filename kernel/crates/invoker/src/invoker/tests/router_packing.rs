// @feature: FP-0.2.一 插件协议 | @ci: rust-test
//! 路由与打包（plugin_scoped_router/host_key/light_packing/reload/boot_assembly）。

use super::*;
#[tokio::test]
async fn test_plugin_scoped_router_injects_plugin_id_into_object_params() {
    let calls = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let scoped: Arc<dyn CapabilityRouter> = Arc::new(PluginScopedRouter {
        plugin_id: "plugin_a".to_string(),
        inner: Arc::new(RecordRouter {
            calls: calls.clone(),
        }),
    });
    let res = scoped
        .handle("metrics", "record", json!({"name": "calls", "value": 1}))
        .await
        .unwrap();
    assert_eq!(res["ok"], true);
    let got = calls.lock().unwrap().clone();
    assert_eq!(got.len(), 1);
    assert_eq!(got[0].2["_plugin_id"], "plugin_a");
    // 原始参数保留
    assert_eq!(got[0].2["name"], "calls");
}
#[tokio::test]
async fn test_plugin_scoped_router_wraps_non_object_params() {
    // 非对象 params（如字符串）→ 包成 {"_plugin_id", "value": params}。
    let calls = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let scoped: Arc<dyn CapabilityRouter> = Arc::new(PluginScopedRouter {
        plugin_id: "plugin_b".to_string(),
        inner: Arc::new(RecordRouter {
            calls: calls.clone(),
        }),
    });
    let _ = scoped.handle("ping", "pong", json!("hello")).await.unwrap();
    let got = calls.lock().unwrap().clone();
    assert_eq!(got[0].2["_plugin_id"], "plugin_b");
    assert_eq!(got[0].2["value"], "hello");
}
#[tokio::test]
async fn test_plugin_scoped_router_known_namespaces_delegates() {
    // known_namespaces 委托给 inner（sidecar initialize 才能拿到插件自注册 namespace）。
    let calls = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let scoped: Arc<dyn CapabilityRouter> = Arc::new(PluginScopedRouter {
        plugin_id: "plugin_c".to_string(),
        inner: Arc::new(RecordRouter { calls }),
    });
    assert_eq!(scoped.known_namespaces(), vec!["custom-ns".to_string()]);
}
#[tokio::test]
async fn test_plugin_scoped_router_scopes_tenant_from_params() {
    // `_tenant_id` 在场 → inner 在该租户 scope 内执行（maintenance 通道实测口径），
    // `_session_id` 随行；两键被桥消费，不透漏给业务 params。
    let (seen, router) = capture_router();
    router
        .handle(
            "pipeline-state",
            "get",
            json!({"key": "task.id", "_tenant_id": "maintenance", "_session_id": "sess-9"}),
        )
        .await
        .unwrap();
    let got = seen.lock().unwrap().clone();
    assert_eq!(got.len(), 1);
    let (tenant, params) = &got[0];
    let tenant = tenant.as_ref().expect("反调必须带租户 scope");
    assert_eq!(tenant.tenant_id, "maintenance", "内核读侧按反调租户解析");
    assert_eq!(tenant.session_id, "sess-9", "session 随行透传");
    assert!(
        params.get("_tenant_id").is_none() && params.get("_session_id").is_none(),
        "桥保留键必须被消费，不透漏给业务 params，实际 {params}"
    );
    // _plugin_id 信任锚点与租户透传共存
    assert_eq!(params["_plugin_id"], "task_evaluate");
    assert_eq!(params["key"], "task.id", "业务参数原样保留");
}
#[tokio::test]
async fn test_plugin_scoped_router_without_tenant_keeps_default_fallback() {
    // 无 `_tenant_id` → 无活跃 task_local（现状语义）：内核读侧
    // current_or_default("default") 回退 default。显式 default 语义锁定（刀1
    // 验收项：反调无租户时 fail-closed 或显式 default 语义有测试锁定）。
    let (seen, router) = capture_router();
    router
        .handle("pipeline-state", "get", json!({"key": "task.id"}))
        .await
        .unwrap();
    let got = seen.lock().unwrap().clone();
    assert_eq!(got.len(), 1);
    let (tenant, _params) = &got[0];
    assert!(
        tenant.is_none(),
        "无 _tenant_id 时不得凭空造租户 scope（回退语义由读侧裁决）"
    );
}
#[tokio::test]
async fn test_plugin_scoped_router_carries_user_identity_from_params() {
    // 刀2（ADR docs/decisions/2026-10-17-reverse-call-user-identity-injection.md）：
    // `_user_id` 在场 → scope 租户回填 user_id（派发面据此以注入身份为准，
    // 插件手写 user 不可指派）；畸形（非字符串/空串）→ user 维缺席但租户
    // scope 照建；保留键一律消费不外漏。
    let (seen, router) = capture_router();
    router
        .handle(
            "chat",
            "send_message",
            json!({
                "pipeline_id": "p1",
                "message": "m",
                "_tenant_id": "maintenance",
                "_session_id": "sess-9",
                "_user_id": "u-real",
            }),
        )
        .await
        .unwrap();
    router
        .handle(
            "chat",
            "send_message",
            json!({
                "pipeline_id": "p2",
                "message": "m",
                "_tenant_id": "maintenance",
                "_user_id": 42,
            }),
        )
        .await
        .unwrap();
    router
        .handle(
            "chat",
            "send_message",
            json!({
                "pipeline_id": "p3",
                "message": "m",
                "_tenant_id": "maintenance",
                "_user_id": "",
            }),
        )
        .await
        .unwrap();
    let got = seen.lock().unwrap().clone();
    assert_eq!(got.len(), 3, "三组有区分度输入（有效/非字符串/空串）");
    let (tenant, params) = &got[0];
    let tenant = tenant.as_ref().expect("有效三键必须建 scope");
    assert_eq!(tenant.user_id.as_deref(), Some("u-real"), "user 维入 scope");
    assert!(
        params.get("_user_id").is_none(),
        "user 保留键被消费，实际 {params}"
    );
    for (tenant, params) in got.iter().skip(1) {
        let tenant = tenant.as_ref().expect("租户键有效，scope 照建");
        assert_eq!(tenant.tenant_id, "maintenance");
        assert!(
            tenant.user_id.is_none(),
            "畸形 user 按缺席（不按畸形值入 scope）"
        );
        assert!(params.get("_user_id").is_none(), "畸形保留键仍被消费");
    }
}
#[tokio::test]
async fn test_plugin_scoped_router_invalid_tenant_shapes_ignored() {
    // 非法形态（非字符串 / 空串）→ 按缺席处理（保守：不按畸形值建 scope），
    // 保留键仍被消费不外漏。
    let (seen, router) = capture_router();
    router
        .handle(
            "metrics",
            "record",
            json!({"name": "n", "value": 1, "_tenant_id": 42}),
        )
        .await
        .unwrap();
    router
        .handle(
            "metrics",
            "record",
            json!({"name": "n", "value": 2, "_tenant_id": ""}),
        )
        .await
        .unwrap();
    let got = seen.lock().unwrap().clone();
    assert_eq!(got.len(), 2, "两组有区分度输入（数字/空串）");
    for (tenant, params) in got.iter() {
        assert!(tenant.is_none(), "畸形租户值不得建 scope");
        assert!(
            params.get("_tenant_id").is_none(),
            "畸形保留键仍被消费，实际 {params}"
        );
    }
}
#[tokio::test]
async fn test_host_key_routing_light_vs_solo() {
    // 宿主键路由（§4.2 第 1 条）：light → 组键；缺省/其他值/外部 MCP/InProcess
    // → 独占键 plugin:{id}（四组有区分度输入一次覆盖）。
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);

    // light sidecar → 首次装箱 group:light:1
    let light = make_light_manifest("guard_a", "python server.py");
    assert_eq!(invoker.resolve_host_key(&light), "group:light:1");

    // 缺省 host_group → 独占键
    let solo = make_sidecar_manifest("heavy_tool", "python server.py");
    assert_eq!(invoker.resolve_host_key(&solo), "plugin:heavy_tool");

    // 任意合法组名声明即准入（多组分域，驱逐爆炸半径按组隔离）：
    // 非 "light" 组名（如稳定面 "light_stable"）→ 自己组的组键
    let mut stable = make_sidecar_manifest("stable_face", "python server.py");
    stable.host_group = Some("light_stable".to_string());
    assert_eq!(
        invoker.resolve_host_key(&stable),
        "group:light_stable:1",
        "声明组名即准入，槽位按组名分域"
    );

    // 非法组名（含冒号破坏 host_key 结构）→ 保守独占
    let mut bad_group = make_sidecar_manifest("bad_group", "python server.py");
    bad_group.host_group = Some("a:b".to_string());
    assert_eq!(
        invoker.resolve_host_key(&bad_group),
        "plugin:bad_group",
        "非法组名缺省保守独占"
    );

    // light 声明但外部 MCP（进程归外部所有）→ 独占键
    let mut ext = make_light_manifest("ext_light", "external");
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
    assert_eq!(invoker.resolve_host_key(&ext), "plugin:ext_light");
}
#[tokio::test]
async fn test_host_key_of_light_cohost_shared_vs_solo_distinct() {
    // host_key_of（域事件广播按宿主去重的投递单位，BUG-7）：同宿主 light 成员
    // 返回同一装箱键；独占插件键含自身互异；manifest 缺席回退 plugin_id。
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader.clone());
    loader.add_manifest(make_light_manifest("mem_a", "python server.py"));
    loader.add_manifest(make_light_manifest("mem_b", "python server.py"));
    loader.add_manifest(make_sidecar_manifest("solo_x", "python server.py"));

    let key_a = invoker.host_key_of("mem_a");
    let key_b = invoker.host_key_of("mem_b");
    assert!(
        key_a.starts_with("group:light:"),
        "light 成员键是组键: {key_a}"
    );
    assert_eq!(key_a, key_b, "装箱同槽的 light 成员共享宿主键");
    assert_eq!(
        invoker.host_key_of("solo_x"),
        "plugin:solo_x",
        "独占插件键含自身"
    );
    assert_ne!(key_a, invoker.host_key_of("solo_x"));
    assert_eq!(
        invoker.host_key_of("ghost"),
        "ghost",
        "manifest 缺席按独占语义回退 plugin_id"
    );
}
#[tokio::test]
async fn test_light_packing_fill_overflow_sticky() {
    // 装箱落点（§4.5）：未满宿主优先塞入 → 溢出开新宿主 → 分配粘性。
    // max_members=2 注入（不碰环境变量）。
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);

    let assign = |pid: &str| invoker.assign_light_host_with(pid, "light", 2);
    // a,b 塞满 slot 1；c,d 塞满 slot 2；e 溢出开 slot 3
    assert_eq!(assign("a"), "group:light:1", "首个成员开新宿主 slot 1");
    assert_eq!(
        assign("b"),
        "group:light:1",
        "未满宿主优先塞入（粘性到 slot 1）"
    );
    assert_eq!(assign("c"), "group:light:2", "slot 1 满 → 溢出开新宿主");
    assert_eq!(assign("d"), "group:light:2", "slot 2 未满继续塞");
    assert_eq!(assign("e"), "group:light:3", "全部满 → 再开新宿主");

    // 分配粘性：已分配成员重复查询返回原宿主（宿主存活期间归属不变）
    assert_eq!(assign("a"), "group:light:1", "粘性：a 归属不变");
    assert_eq!(assign("c"), "group:light:2", "粘性：c 归属不变");
    // 成员集反查：slot 1 恰含 {a,b}（排序）
    assert_eq!(
        invoker.host_members("group:light:1"),
        vec!["a".to_string(), "b".to_string()]
    );
}
#[tokio::test]
async fn test_light_packing_slot_reuse_after_reclaim() {
    // 槽位复用（§4.5 第 3 条）：宿主被回收（分配表条目清空）后槽位空出，
    // 后续新插件装箱优先复用该宿主，而不是无限开新组。
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);

    let assign = |pid: &str| invoker.assign_light_host_with(pid, "light", 2);
    assert_eq!(assign("a"), "group:light:1");
    assert_eq!(assign("b"), "group:light:1");
    assert_eq!(assign("c"), "group:light:2");
    assert_eq!(assign("d"), "group:light:2");
    assert_eq!(assign("e"), "group:light:3");

    // idle GC 回收 slot 1（reclaim=true 清分配表成员条目）
    invoker.unload_host("group:light:1", true).await.unwrap();
    assert!(
        invoker.host_members("group:light:1").is_empty(),
        "回收后分配表成员条目必须清空"
    );

    // 新成员 f 落点 = 复用 slot 1（计数归 0 优先），不开 slot 4
    assert_eq!(assign("f"), "group:light:1", "回收槽位优先复用");
    // 被回收的老成员 a 重新分配也回 slot 1（分配条目已清，按未满规则落点）
    assert_eq!(assign("a"), "group:light:1");
}
#[tokio::test]
async fn test_multi_group_packing_isolates_groups() {
    // 多组分域（合宿多组扩展）：不同组名各自独立装箱计数与槽位序列——
    // 易变组（light）溢出开新槽不影响稳定组（light_stable）落点；驱逐按
    // 宿主键连坐天然不跨组（unload_host 只杀本组宿主、只清本组成员条目）。
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);

    let assign_light = |pid: &str| invoker.assign_light_host_with(pid, "light", 2);
    let assign_stable = |pid: &str| invoker.assign_light_host_with(pid, "light_stable", 2);

    // 易变组塞满 slot 1
    assert_eq!(assign_light("a"), "group:light:1");
    assert_eq!(assign_light("b"), "group:light:1");
    // 稳定组首成员落自己组的 slot 1，不接手易变组的计数（独立分域）
    assert_eq!(
        assign_stable("mon"),
        "group:light_stable:1",
        "稳定组槽位序列独立于易变组"
    );
    // 易变组溢出开 slot 2，稳定组 slot 1 仍有空位可继续塞
    assert_eq!(assign_light("c"), "group:light:2");
    assert_eq!(assign_stable("cost"), "group:light_stable:1");

    // 成员集反查按组键各归其位
    assert_eq!(
        invoker.host_members("group:light_stable:1"),
        vec!["cost".to_string(), "mon".to_string()]
    );
    assert_eq!(
        invoker.host_members("group:light:1"),
        vec!["a".to_string(), "b".to_string()]
    );

    // 驱逐隔离：回收易变组 slot 1，稳定组成员条目原样保留
    invoker.unload_host("group:light:1", true).await.unwrap();
    assert_eq!(
        invoker.host_members("group:light_stable:1"),
        vec!["cost".to_string(), "mon".to_string()],
        "易变组驱逐不得波及稳定组成员"
    );
}
#[tokio::test]
async fn test_packing_repacks_when_declared_group_changes() {
    // 搬组（多组扩展的粘性修正）：manifest 改组名（light → light_stable）后，
    // 旧装箱条目跨组过期——继续粘在旧组会让成员继续承受旧组驱逐连坐。
    // 装箱判定必须按当前声明组重装箱（旧宿主成员集随之收缩）。
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);

    assert_eq!(
        invoker.assign_light_host_with("mon", "light", 6),
        "group:light:1",
        "初始落易变组"
    );

    // 声明组变更 → 重装箱进新组（旧组条目被覆盖，成员集不再包含 mon）
    assert_eq!(
        invoker.assign_light_host_with("mon", "light_stable", 6),
        "group:light_stable:1",
        "组名变更后按新组重装箱"
    );
    assert_eq!(
        invoker.host_members("group:light:1"),
        Vec::<String>::new(),
        "旧组成员集收缩"
    );
    assert_eq!(
        invoker.host_members("group:light_stable:1"),
        vec!["mon".to_string()]
    );

    // 同组重复查询保持粘性（不因搬组判定破坏既有粘着语义）
    assert_eq!(
        invoker.assign_light_host_with("mon", "light_stable", 6),
        "group:light_stable:1"
    );
}
#[tokio::test]
async fn test_reload_member_paths_without_live_host() {
    // reload_member 三路判定：独占 → Err（watcher 回退 force_unload）；
    // 合宿未装箱（从未调用）→ Ok(no-op)；已装箱但宿主未 spawn → Ok(no-op)
    // ——无存活进程即无进程内代码，下次 spawn 必用新码，无需驱逐任何东西。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_sidecar_manifest("solo_p", "python server.py"));
    loader.add_manifest(make_light_manifest("fresh_m", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);

    assert!(
        invoker.reload_member("solo_p").await.is_err(),
        "独占插件不支持进程内重载（独占本就无连坐面）"
    );
    assert!(
        invoker.reload_member("fresh_m").await.is_ok(),
        "未装箱成员 = 无宿主 = no-op"
    );
    invoker.assign_light_host_with("fresh_m", "light", 6);
    assert!(
        invoker.reload_member("fresh_m").await.is_ok(),
        "装箱但宿主未 spawn = no-op（下次 spawn 必新码）"
    );
}
#[tokio::test]
async fn test_reload_member_end_to_end_with_real_host() {
    // 端到端（真 host.py 合宿宿主）：装箱 → 真 SDK 宿主进程 spawn 入缓存 →
    // reload_member 发 agentos/reload_member 请求 → 宿主进程内重载成员并
    // 应答 → 指纹记账刷新为当前并集（防 pull 路径误判 stale 整组 respawn）。
    // python/SDK 不可用时跳过（CI 环境防御，与 mcp crate 探针约定同款）。
    let ok = std::process::Command::new(python_exe())
        .arg("-c")
        .arg("import agentos_plugin_sdk, mcp")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    if !ok {
        eprintln!("python 或 agentos_plugin_sdk 不可用，跳过 reload 端到端");
        return;
    }

    // 临时 shared_root：单成员 a_service（含 probe 工具），驱动脚本以
    // shared_root 注入启动真 host.py。
    let tmp = tempfile::tempdir().unwrap();
    let member_dir = tmp.path().join("tools").join("a_service");
    std::fs::create_dir_all(&member_dir).unwrap();
    std::fs::write(member_dir.join("plugin.json"), r#"{"id": "a_service"}"#).unwrap();
    std::fs::write(
        member_dir.join("server.py"),
        concat!(
            "from agentos_plugin_sdk import AgentOSPlugin\n",
            "plugin = AgentOSPlugin(\"a_service\")\n",
            "@plugin.tool(name='probe', schema={'type': 'object'})\n",
            "async def probe() -> dict:\n",
            "    return {'value': 1}\n",
        ),
    )
    .unwrap();
    let host_py = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../plugins/shared/_host/host.py");
    let shared_root = tmp.path().to_string_lossy().replace('\\', "/");
    let driver = format!(
        r#"
import sys
sys.path.insert(0, r"{host_py_dir}")
import host
sys.exit(host.main(["--group", "light", "--slot", "1", "--members", "a_service"],
                   shared_root=__import__("pathlib").Path(r"{shared_root}")))
"#,
        host_py_dir = host_py.parent().unwrap().display(),
        shared_root = shared_root,
    );

    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_light_manifest("a_service", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);
    // 装箱（分配表落 group:light:1）
    invoker.assign_light_host_with("a_service", "light", 6);

    let mut client = McpClient::new_stdio(python_exe().to_string(), vec!["-c".to_string(), driver]);
    client.connect().await.expect("真宿主 spawn 应成功");
    client
        .initialize(&serde_json::json!({}))
        .await
        .expect("握手应成功");
    let host_key = "group:light:1".to_string();
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::new(tokio::sync::RwLock::new(client)));
    // 预置过期指纹（陈旧并集）——reload 成功后必须刷新为当前并集
    invoker
        .fingerprints
        .write()
        .insert(host_key.clone(), (42u64, Instant::now()));

    invoker
        .reload_member("a_service")
        .await
        .expect("reload_member 应成功（宿主进程内重载 + 应答）");

    let (fp, _) = *invoker
        .fingerprints
        .read()
        .get(&host_key)
        .expect("指纹条目应存在");
    assert_eq!(
        fp,
        invoker.host_union_fingerprint(&host_key),
        "reload 后指纹记账必须等于当前成员并集（防 stale 误判整组 respawn）"
    );
    invoker.shutdown_all().await;
}
#[tokio::test]
async fn test_unload_member_end_to_end_with_real_host() {
    // 端到端（真 host.py 合宿宿主）：双成员宿主 → unload_member 进程内摘除
    // a_service（其余成员不动）→ 内核记账同步收缩（spawned_members 快照与
    // 分配表同摘、成员空闲时钟清除、并集指纹刷新）→ 服务面收缩（tools/list
    // 不再含被卸成员工具）且其余成员工具照常可达；对最后成员再卸 → Err
    // （内核回退整组回收），宿主进程全程存活。python/SDK 不可用时跳过。
    let ok = std::process::Command::new(python_exe())
        .arg("-c")
        .arg("import agentos_plugin_sdk, mcp")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    if !ok {
        eprintln!("python 或 agentos_plugin_sdk 不可用，跳过 unload 端到端");
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
    invoker.touch_member_last_used("a_service");
    invoker.touch_member_last_used("b_service");

    invoker
        .unload_group_member("a_service")
        .await
        .expect("unload_member 应成功（宿主进程内摘除 + 应答）");

    // 记账收缩：spawned_members 快照摘除（进程服务面收缩）、分配条目保留
    // （待重加入标记，宿主重载风暴 2026-09-25 R1）、成员空闲时钟清除、
    // 并集指纹刷新（分配表未变，指纹保持 spawn 期望态）
    assert_eq!(
        invoker.spawned_members.read().get(&host_key).cloned(),
        Some(vec!["b_service".to_string()]),
        "spawn 成员集快照必须同步收缩"
    );
    assert!(
        invoker
            .light_packing
            .read()
            .assignments
            .contains_key("a_service"),
        "被卸成员的分配条目保留（待重加入：随宿主下次 respawn 一次性回归）"
    );
    assert!(
        invoker
            .light_packing
            .read()
            .assignments
            .contains_key("b_service"),
        "其余成员分配不受影响"
    );
    assert!(!invoker.member_last_used.read().contains_key("a_service"));
    assert!(invoker.member_last_used.read().contains_key("b_service"));
    let (fp, _) = *invoker
        .fingerprints
        .read()
        .get(&host_key)
        .expect("指纹条目应存在");
    assert_eq!(
        fp,
        invoker.host_union_fingerprint(&host_key),
        "成员卸载后指纹记账必须等于当前成员并集（防 stale 误判整组 respawn）"
    );

    // 宿主进程存活且服务面收缩：被卸成员工具不可达，其余成员工具照常调用
    assert!(shared.read().await.is_alive().await, "宿主进程必须存活");
    let tools = shared.read().await.list_tools().await.expect("tools/list");
    let tools_str = serde_json::to_string(&tools).unwrap();
    assert!(
        !tools_str.contains("a_service.probe"),
        "被卸成员工具必须退出服务面: {tools_str}"
    );
    assert!(
        tools_str.contains("b_service.probe"),
        "其余成员工具保持可达: {tools_str}"
    );
    let called = shared
        .read()
        .await
        .call_tool("b_service.probe", &serde_json::json!({}))
        .await
        .expect("剩余成员工具必须照常可调");
    assert!(
        serde_json::to_string(&called).unwrap().contains("value"),
        "工具应答须含探针值"
    );

    // 对最后成员再卸 → Err（宿主侧拒绝空成员集；内核回退整组回收路径）
    let err = invoker
        .unload_group_member("b_service")
        .await
        .expect_err("最后成员必须拒绝成员级卸载");
    assert!(err.message.contains("最后成员"), "实际: {}", err.message);
    assert!(
        shared.read().await.is_alive().await,
        "拒绝路径不得破坏宿主状态"
    );
    invoker.shutdown_all().await;
}
#[tokio::test]
async fn test_member_sync_baseline_desync_must_not_kill_host() {
    // P0 主复现：补齐成功（镜像=分配表）后，任一成员的下一次复用必须命中
    // 缓存——不得以「并集变了 + 热换无对象」整组驱逐存活宿主。
    let (invoker, host_key, live_arc) = setup_synced_host_with_stale_union_baseline().await;
    let manifest = invoker
        .loader
        .get_manifest("mem_a")
        .expect("前置：mem_a manifest 在册");

    let got = invoker
        .reuse_cached_host_fast_path(&host_key, &manifest)
        .await;

    let client = got.expect("成员集补齐后内容一致：必须复用存活宿主，不得整组重载");
    assert!(
        Arc::ptr_eq(&client, &live_arc),
        "必须复用原客户端实例（不得驱逐重建）"
    );
    assert!(
        invoker.mcp_clients.read().get(&host_key).is_some(),
        "缓存条目必须保留"
    );
    assert!(
        live_arc.read().await.is_alive().await,
        "宿主进程必须存活（不得 kill）"
    );
    assert_eq!(
        invoker
            .fingerprints
            .read()
            .get(&host_key)
            .map(|(fp, _)| *fp),
        Some(invoker.host_union_fingerprint(&host_key)),
        "并集基线必须刷新为当前成员并集（消除失步源头）"
    );
    invoker.shutdown_all().await;
}
#[tokio::test]
async fn test_hot_swap_nochange_refreshes_baseline_and_setchanged_still_reloads() {
    // 成员级热换判定直测（两组有区分度输入）：
    // - NoChange（每成员表与磁盘逐成员一致）：返回 true 免重载 + 刷新并集基线
    //   ——stale 信号是基线失步不是真变更；
    // - SetChanged（快照缺新成员）：返回 false 回退整组重建（行为保留）。
    let (invoker, host_key, _live) = setup_synced_host_with_stale_union_baseline().await;

    let swapped = invoker.try_member_level_hot_swap(&host_key).await;
    assert!(
        swapped,
        "逐成员内容一致：热换必须免重载返回 true（基线刷新即愈）"
    );
    assert_eq!(
        invoker
            .fingerprints
            .read()
            .get(&host_key)
            .map(|(fp, _)| *fp),
        Some(invoker.host_union_fingerprint(&host_key)),
        "免重载路径必须刷新并集基线（否则下一调用重蹈 stale）"
    );

    // 对照组：spawn 快照缺 c（真成员集变化）→ 仍回退整组重建
    invoker.host_member_fingerprints.write().insert(
        host_key.clone(),
        ["mem_a", "mem_b"]
            .iter()
            .map(|p| (p.to_string(), 0u64))
            .collect(),
    );
    let swapped_set_changed = invoker.try_member_level_hot_swap(&host_key).await;
    assert!(
        !swapped_set_changed,
        "成员集变化（快照缺新成员）必须维持整组重建语义"
    );
    invoker.shutdown_all().await;
}
#[test]
fn light_packing_decisions_are_insertion_order_stable() {
    // 装箱判定稳定性（打包期公理同源）：**同一插入序**下装箱结果逐位确定
    //（槽位计数=数值聚合，与本表物理迭代序无关）；**同一最终分配表**的派生面
    //（host_members/并集指纹）与物理迭代序无关（排序拷贝）。不同插入序允许
    // 不同槽位归属（§4.5「装箱顺序即序号」，by-design 非缺陷）。
    let build = |scrambled: bool| {
        let loader = Arc::new(MockLoader::new());
        let invoker = PluginInvokerImpl::new(loader);
        let ids: Vec<String> = (0..12).map(|i| format!("plug_{i:02}")).collect();
        if scrambled {
            // 乱序插入（偶数槽先、奇数槽后，与升序严格不同序）
            let even: Vec<String> = (0..12).step_by(2).map(|i| format!("plug_{i:02}")).collect();
            let odd: Vec<String> = (1..12).step_by(2).map(|i| format!("plug_{i:02}")).collect();
            for id in even.into_iter().chain(odd) {
                invoker.assign_light_host_with(&id, "light", 6);
            }
        } else {
            for id in &ids {
                invoker.assign_light_host_with(id, "light", 6);
            }
        }
        let members: Vec<String> = invoker.host_members("group:light:1");
        let union = invoker.host_union_fingerprint("group:light:1");
        (members, union)
    };
    // 同一插入序两遍：结果逐位确定（跨轮次/跨进程无随机性）
    let (a_members, a_union) = build(false);
    let (b_members, b_union) = build(false);
    assert_eq!(
        a_members, b_members,
        "同插入序：装箱结果必须逐位确定（槽位计数数值聚合，无随机源）"
    );
    assert_eq!(a_union, b_union, "同插入序：并集指纹必须一致");
    let mut sorted = a_members.clone();
    sorted.sort();
    assert_eq!(a_members, sorted, "成员集派生恒为键升序");
}
#[tokio::test]
async fn boot_assembly_shape_survives_chat_capability_invalidation() {
    // boot 装配形态回归（2026-11-01 真机 boot 回归锁定面）：boot 末段
    // invalidate_all_sidecars（chat.send_message 注册后全量废弃缓存宿主，
    // main.rs 缺陷E 收口）必须**只清进程面记账**——装箱分配表（权威）与
    // spawn 每成员指纹表不得被扰动，重预热再装箱幂等。任何令 boot 装配序
    // 丢失分配/扰动记账的实现回归都会在此变红。
    // 槽容量钉 1024（环境变量优先于档位派生）：全体成员装入单宿主，
    // 断言不依赖测试机档位探测。
    let _cap = EnvVarGuard::set("AGENTOS_LIGHT_HOST_MAX_MEMBERS", "1024");
    let loader = Arc::new(MockLoader::new());
    for i in 0..24 {
        loader.add_manifest(make_light_manifest(
            &format!("plug_{i:02}"),
            "python server.py",
        ));
    }
    let invoker = Arc::new(PluginInvokerImpl::new(loader));

    // boot 预热预分配（sidecar_warmup 同款：先分配完全体再并发 warmup）
    for i in 0..24 {
        invoker.preassign_host_key(&make_light_manifest(
            &format!("plug_{i:02}"),
            "python server.py",
        ));
    }
    let members_before = invoker.host_members("group:light:1");
    assert_eq!(members_before.len(), 24, "前置：全体成员已装箱单宿主");
    let assignments_before: Vec<(String, String)> = invoker
        .light_packing
        .read()
        .assignments
        .iter()
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();

    // spawn 每成员指纹表落基线（spawn 同点记账）后走 boot 末段全量废弃
    invoker.snapshot_member_fingerprints("group:light:1");
    invoker.invalidate_all_sidecars("test: chat capability registered");

    assert_eq!(
        invoker.host_members("group:light:1"),
        members_before,
        "全量废弃不得扰动装箱权威（分配表粘性）"
    );
    let assignments_after: Vec<(String, String)> = invoker
        .light_packing
        .read()
        .assignments
        .iter()
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    assert_eq!(
        assignments_before, assignments_after,
        "全量废弃不得增删改任何分配条目"
    );
    assert!(
        invoker.mcp_clients.read().is_empty() && invoker.spawned_members.read().is_empty(),
        "进程面记账（缓存/镜像）必须全清（respawn 重建语义）"
    );

    // 重预热再装箱幂等：同一成员集拿到同一宿主键（粘性）
    for i in 0..24 {
        let key = invoker.assign_light_host_with(&format!("plug_{i:02}"), "light", 1024);
        assert_eq!(key, "group:light:1", "重装箱必须命中既有分配（粘性）");
    }
}
