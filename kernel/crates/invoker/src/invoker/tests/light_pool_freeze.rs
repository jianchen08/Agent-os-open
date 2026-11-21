// @feature: FP-0.2.一 插件协议 | @ci: rust-test
//! light 合宿与池治理（group_host/warmup/keep_warm/snapshots/freeze_dump/pool/timeout）。

use super::*;
#[tokio::test]
async fn test_resolve_group_host_command_contract() {
    // 合宿宿主 spawn 参数契约（§4.2 第 2 条）：`python host.py --group light
    // --slot {n} --members {逗号分隔成员列表}`，工作目录 plugins/shared/_host/，
    // 解释器 = _host 共享 venv（uv 单轨 fail-closed）。
    let tmp = tempfile::tempdir().unwrap();
    // 造合成成员目录（任意深度皆可：拍平生产布局为 plugins/shared/<id> 一层，
    // 本测试证 spawn 契约与层级无关）+ _host/.venv 布局
    let member_dir = tmp.path().join("pipeline").join("input").join("guard_a");
    std::fs::create_dir_all(&member_dir).unwrap();
    let host_dir = tmp.path().join("_host");
    let interp = fake_venv(&host_dir, true);
    std::fs::write(host_dir.join("host.py"), b"# host stub").unwrap();

    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "a_guard".to_string(),
        member_dir.to_string_lossy().into_owned(),
    );
    loader.plugin_dirs.write().insert(
        "b_guard".to_string(),
        member_dir.to_string_lossy().into_owned(),
    );
    let invoker = PluginInvokerImpl::new(loader);

    let (cmd, args, workdir) = invoker
        .resolve_group_host_command(
            "light",
            3,
            &["b_guard".to_string(), "a_guard".to_string()],
            None,
        )
        .expect("参数契约解析应成功");
    assert_eq!(
        cmd,
        interp.to_string_lossy().into_owned(),
        "解释器 = _host 共享 venv"
    );
    assert_eq!(
        args,
        vec![
            "host.py".to_string(),
            "--group".to_string(),
            "light".to_string(),
            "--slot".to_string(),
            "3".to_string(),
            "--members".to_string(),
            // 成员列表排序（稳定契约：指纹/spawn/观测共用同一序）
            "a_guard,b_guard".to_string(),
        ]
    );
    assert_eq!(workdir, host_dir.to_string_lossy().into_owned());

    // fail-closed：_host 目录缺失 → HOST_DIR_NOT_FOUND
    let empty_loader = Arc::new(MockLoader::new());
    let empty_dir = tempfile::tempdir().unwrap();
    empty_loader.plugin_dirs.write().insert(
        "ghost".to_string(),
        empty_dir.path().to_string_lossy().into_owned(),
    );
    let invoker2 = PluginInvokerImpl::new(empty_loader);
    let err = invoker2
        .resolve_group_host_command("light", 1, &["ghost".to_string()], None)
        .expect_err("无 _host 目录必须 fail-closed");
    assert_eq!(err.code.as_deref(), Some("HOST_DIR_NOT_FOUND"));

    // fail-closed：_host 存在但共享 venv 缺失 → HOST_VENV_MISSING
    // （用户根钉空目录：登记处 _host 兜底缺席的判定与机器环境无关）
    let bare_host = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(bare_host.path().join("_host")).unwrap();
    let bare_member = bare_host.path().join("m");
    std::fs::create_dir_all(&bare_member).unwrap();
    let empty_user = tempfile::tempdir().unwrap();
    let _guard = UserRootGuard::pin_to(empty_user.path());
    let loader3 = Arc::new(MockLoader::new());
    loader3
        .plugin_dirs
        .write()
        .insert("m".to_string(), bare_member.to_string_lossy().into_owned());
    let invoker3 = PluginInvokerImpl::new(loader3);
    let err3 = invoker3
        .resolve_group_host_command("light", 1, &["m".to_string()], None)
        .expect_err("共享 venv 缺失必须 fail-closed");
    assert_eq!(err3.code.as_deref(), Some("HOST_VENV_MISSING"));
}
#[test]
fn test_resolve_group_host_command_falls_back_to_user_space_venv() {
    // 解析序②（共享宿主侧）：宿主目录 .venv 缺席（装机态）+ 登记处
    // plugin-venvs/_host 有共享解释器 → 解释器取登记处，工作目录仍 = 宿主
    // 目录（host.py 在包内是种子，venv 是派生数据——两者落点解耦）。
    let tmp = tempfile::tempdir().unwrap();
    let host_dir = tmp.path().join("_host");
    std::fs::create_dir_all(&host_dir).unwrap();
    std::fs::write(host_dir.join("host.py"), b"# host stub").unwrap();
    let member_dir = tmp.path().join("pipeline").join("input").join("guard_a");
    std::fs::create_dir_all(&member_dir).unwrap();

    let user_root = tempfile::tempdir().unwrap();
    let _guard = UserRootGuard::pin_to(user_root.path());
    let registered = fake_registered_venv(user_root.path(), GROUP_HOST_DIR);

    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "a_guard".to_string(),
        member_dir.to_string_lossy().into_owned(),
    );
    let invoker = PluginInvokerImpl::new(loader);

    let (cmd, _args, workdir) = invoker
        .resolve_group_host_command("light", 1, &["a_guard".to_string()], None)
        .expect("登记处共享解释器必须被命中");
    assert_eq!(cmd, registered.to_string_lossy().into_owned());
    assert_eq!(
        workdir,
        host_dir.to_string_lossy().into_owned(),
        "工作目录不变：host.py 仍在宿主目录（种子），仅解释器来自用户空间"
    );
}
#[test]
fn test_resolve_group_host_command_falls_back_to_builtin_root_host_dir() {
    // 回退场景①：全组成员落非内置根（模式包用户副本赢后合宿组全落用户空间，
    // 祖先链无 _host）→ 探测全空回退内置根 _host/，workdir/解释器 = 内置根侧
    // 原件（运行环境 junction 补救的内核侧永久化）。
    let builtin = tempfile::tempdir().unwrap();
    let builtin_host = builtin.path().join(GROUP_HOST_DIR);
    let builtin_interp = fake_venv(&builtin_host, true);
    std::fs::write(builtin_host.join("host.py"), b"# host stub").unwrap();

    // 用户空间成员：独立临时根下 modes/mode_x，向上到文件系统根均无 _host
    let user_space = tempfile::tempdir().unwrap();
    let member_dir = user_space.path().join("modes").join("mode_x");
    std::fs::create_dir_all(&member_dir).unwrap();

    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "mode_x".to_string(),
        member_dir.to_string_lossy().into_owned(),
    );
    let invoker = PluginInvokerImpl::new(loader);

    let (cmd, args, workdir) = invoker
        .resolve_group_host_command("light", 1, &["mode_x".to_string()], Some(builtin.path()))
        .expect("探测全空必须回退内置根 _host/");
    assert_eq!(
        workdir,
        builtin_host.to_string_lossy().into_owned(),
        "回退命中内置根 _host/"
    );
    assert_eq!(
        cmd,
        builtin_interp.to_string_lossy().into_owned(),
        "解释器 = 内置根共享 venv"
    );
    assert_eq!(
        args,
        vec![
            "host.py".to_string(),
            "--group".to_string(),
            "light".to_string(),
            "--slot".to_string(),
            "1".to_string(),
            "--members".to_string(),
            "mode_x".to_string(),
        ]
    );

    // 回退位无 _host → 回退亦未命中，fail-closed 不变（回退不是兜底放行）
    let bare_builtin = tempfile::tempdir().unwrap();
    let err = invoker
        .resolve_group_host_command(
            "light",
            1,
            &["mode_x".to_string()],
            Some(bare_builtin.path()),
        )
        .expect_err("内置根回退位无 _host 必须照旧 fail-closed");
    assert_eq!(err.code.as_deref(), Some("HOST_DIR_NOT_FOUND"));
}
#[test]
fn test_resolve_group_host_command_member_probe_wins_over_builtin_fallback() {
    // 回退场景②：成员在内置根（仓库侧现状布局）→ 向上探测命中最近祖先
    // _host，行为与旧路径一致；回退位放另一份 _host（不同解释器）即可判
    // 优先序——探测序在前，回退位不被误用。
    let builtin = tempfile::tempdir().unwrap();
    let member_host = builtin.path().join(GROUP_HOST_DIR);
    let member_interp = fake_venv(&member_host, true);
    std::fs::write(member_host.join("host.py"), b"# host stub").unwrap();

    let decoy_root = tempfile::tempdir().unwrap();
    let decoy_interp = fake_venv(&decoy_root.path().join(GROUP_HOST_DIR), true);
    assert_ne!(
        member_interp, decoy_interp,
        "两份 _host 解释器路径必须可区分，否则判不了优先序"
    );

    // 成员目录：内置根下 pipeline/input/guard_a（仓库侧 plugins/shared 布局）
    let member_dir = builtin
        .path()
        .join("pipeline")
        .join("input")
        .join("guard_a");
    std::fs::create_dir_all(&member_dir).unwrap();

    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "guard_a".to_string(),
        member_dir.to_string_lossy().into_owned(),
    );
    let invoker = PluginInvokerImpl::new(loader);

    let (cmd, _, workdir) = invoker
        .resolve_group_host_command(
            "light",
            1,
            &["guard_a".to_string()],
            Some(decoy_root.path()),
        )
        .expect("成员祖先链 _host 命中时不得走回退");
    assert_eq!(
        workdir,
        member_host.to_string_lossy().into_owned(),
        "最近祖先 _host 优先（旧路径行为零变化）"
    );
    assert_eq!(cmd, member_interp.to_string_lossy().into_owned());
}
#[test]
fn test_namespaced_tool_name_prefix() {
    // 调用路由前缀（§4.2 第 3 条）：light 成员工具名拼 {plugin_id}. 前缀；
    // 独占宿主（含 light 声明但外部 MCP 形态）无前缀。
    let light = make_light_manifest("pause_guard", "python server.py");
    assert_eq!(namespaced_tool_name(&light, "check"), "pause_guard.check");
    // invoke_entry 本身带点号也整体作为后缀（前缀机制与入口名正交）
    assert_eq!(
        namespaced_tool_name(&light, "pause_guard.check"),
        "pause_guard.pause_guard.check"
    );

    let solo = make_sidecar_manifest("llm_core", "python server.py");
    assert_eq!(
        namespaced_tool_name(&solo, "llm_core.execute"),
        "llm_core.execute"
    );

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
    assert_eq!(
        namespaced_tool_name(&ext, "remote_tool"),
        "remote_tool",
        "外部 MCP 不进合宿组 → 无前缀"
    );
}
#[tokio::test]
async fn test_member_idle_thresholds_are_per_member() {
    // 成员粒度阈值（ADR 2026-09-24 取代 §4.8 整组 max 聚合）：每个成员按自己
    // 的声明（0 = 永不卸载 / 自定义值 / 默认 300）独立判定，不再取组内最大。
    // 与 env 突变用例互斥：无生命周期成员的阈值依赖"默认 300"这一全局基线。
    let _serial = ENV_SENSITIVE_LOCK.lock();
    let loader = Arc::new(MockLoader::new());
    let mut never = make_sidecar_manifest("never_idle", "python server.py");
    never.lifecycle = Some(agentos_core::traits::PluginLifecycle {
        idle_timeout_secs: Some(0),
    });
    let mut custom = make_sidecar_manifest("custom_idle", "python server.py");
    custom.lifecycle = Some(agentos_core::traits::PluginLifecycle {
        idle_timeout_secs: Some(42),
    });
    loader.add_manifest(never);
    loader.add_manifest(custom);
    loader.add_manifest(make_sidecar_manifest("default_idle", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);

    // 各成员阈值只由自身声明决定（互不影响）
    assert_eq!(invoker.idle_timeout_secs_sync("never_idle"), 0);
    assert_eq!(invoker.idle_timeout_secs_sync("custom_idle"), 42);
    assert_eq!(
        invoker.idle_timeout_secs_sync("default_idle"),
        agentos_core::traits::default_idle_timeout()
    );
}
#[test]
fn test_light_host_max_members_env_and_default() {
    // 上限配置（§4.5）：AGENTOS_LIGHT_HOST_MAX_MEMBERS 覆盖；未设/无效值回退
    // 设备档位派生（P2 接线：手机全成员装入 / 桌面收敛值）。回退断言用生产
    // 同源表达式（slot_capacity_for_tier(detect_device_tier())）而非字面量
    // ——派生值随宿主机档位而变，字面量会在手机档宿主机上误红。
    // 仅本测试触碰该环境变量（其他装箱测试全部走 assign_light_host_with 注入
    // 或以同源表达式自洽推导），无并发污染。
    std::env::set_var("AGENTOS_LIGHT_HOST_MAX_MEMBERS", "3");
    assert_eq!(light_host_max_members(), 3);
    std::env::set_var("AGENTOS_LIGHT_HOST_MAX_MEMBERS", "0");
    assert_eq!(
        light_host_max_members(),
        slot_capacity_for_tier(agentos_core::device_profile::detect_device_tier()),
        "无效值（0）回退档位派生"
    );
    std::env::set_var("AGENTOS_LIGHT_HOST_MAX_MEMBERS", "not-a-number");
    assert_eq!(
        light_host_max_members(),
        slot_capacity_for_tier(agentos_core::device_profile::detect_device_tier()),
        "非数字回退档位派生"
    );
    std::env::remove_var("AGENTOS_LIGHT_HOST_MAX_MEMBERS");
    assert_eq!(
        light_host_max_members(),
        slot_capacity_for_tier(agentos_core::device_profile::detect_device_tier())
    );
}
#[test]
fn test_slot_capacity_follows_device_tier() {
    // 档位 → 槽容量纯函数：手机档全成员装入（上限兜底，槽位结构性 ∈[1,2]）；
    // 桌面/云档收敛值 20（2×light + stable + longwait = 4 宿主的前提）。
    use agentos_core::device_profile::DeviceTier;
    assert_eq!(slot_capacity_for_tier(DeviceTier::Phone), 1024);
    assert!(
        slot_capacity_for_tier(DeviceTier::Phone) >= 67,
        "≥ light 组声明面全集"
    );
    assert_eq!(slot_capacity_for_tier(DeviceTier::Desktop), 20);
    assert_eq!(
        slot_capacity_for_tier(DeviceTier::Cloud),
        20,
        "云与桌面同值（现役部署同律）"
    );
}
#[test]
fn test_cohost_packing_domain_folds_on_phone_only() {
    // 装箱域纯函数：手机档全组折入 light 单域（组数≠宿主数下界，1-2 可达）；
    // 桌面/云档按声明组分域（隔离语义保留）。
    use agentos_core::device_profile::DeviceTier;
    for declared in ["light_stable", "longwait", "light", "custom_group"] {
        assert_eq!(
            cohost_packing_domain(declared, DeviceTier::Phone),
            "light",
            "手机档 {declared} 折入 light 单域"
        );
        assert_eq!(
            cohost_packing_domain(declared, DeviceTier::Desktop),
            declared,
            "桌面档 {declared} 保持声明域"
        );
        assert_eq!(
            cohost_packing_domain(declared, DeviceTier::Cloud),
            declared,
            "云档 {declared} 保持声明域"
        );
    }
}
#[tokio::test]
async fn test_phone_shape_packing_single_domain_all_members() {
    // 手机档装箱形态（经参数化装箱面验证折叠决策的最终效果）：不同声明组
    // 成员同落 group:light:1，槽容量不构成切分（全成员装入）。
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);
    let cap = slot_capacity_for_tier(agentos_core::device_profile::DeviceTier::Phone);
    let domain = cohost_packing_domain(
        "light_stable",
        agentos_core::device_profile::DeviceTier::Phone,
    );
    assert_eq!(
        invoker.assign_light_host_with("tool_a", domain, cap),
        "group:light:1"
    );
    assert_eq!(
        invoker.assign_light_host_with("monitor_b", domain, cap),
        "group:light:1"
    );
    assert_eq!(
        invoker.assign_light_host_with("interaction_c", domain, cap),
        "group:light:1",
        "声明组各异（stable/longwait/light）成员手机档同宿主"
    );
    assert_eq!(
        invoker.host_members("group:light:1").len(),
        3,
        "全成员装入单宿主"
    );
}
#[tokio::test]
async fn test_kill_sidecar_if_any_light_kills_whole_host() {
    // disable 窄口 kill 的合宿语义：kill 整宿主（进程所有权单位），分配表保留
    // （reenable 后同组成员回到原宿主 respawn）。
    let loader = Arc::new(MockLoader::new());
    let manifest_a = make_light_manifest("guard_a", "python server.py");
    let manifest_b = make_light_manifest("guard_b", "python server.py");
    loader.add_manifest(manifest_a.clone());
    loader.add_manifest(manifest_b.clone());
    let invoker = PluginInvokerImpl::new(loader);

    let host_key = invoker.resolve_host_key(&manifest_a);
    invoker.resolve_host_key(&manifest_b);
    assert_eq!(host_key, "group:light:1");
    let live = spawn_long_lived_stdio_client().await;
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));

    invoker.kill_sidecar_if_any("guard_a").await;

    wait_until_killed(&live_arc, "disable 成员必须 kill 整宿主进程").await;
    assert!(invoker.mcp_clients.read().get(&host_key).is_none());
    // 分配表保留（窄口语义：不做指纹/分配清理，reenable 后 respawn 回原宿主）
    assert_eq!(
        invoker.light_packing.read().assignments.len(),
        2,
        "窄口 kill 不清分配表"
    );
}
#[tokio::test]
async fn test_force_unload_light_kills_host_keeps_assignments() {
    // force_unload（热重载语境）对 light 成员：kill 整宿主 + 清缓存/指纹/
    // last_used，但**保留分配表**——下次调用按表 respawn 同一宿主（§4.5 第 2
    // 条"respawn 按表重建成员集"）。
    let loader = Arc::new(MockLoader::new());
    let manifest_a = make_light_manifest("guard_a", "python server.py");
    loader.add_manifest(manifest_a.clone());
    let invoker = PluginInvokerImpl::new(loader);

    let host_key = invoker.resolve_host_key(&manifest_a);
    let live = spawn_long_lived_stdio_client().await;
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));
    invoker.touch_member_last_used("guard_a");

    invoker.force_unload_impl("guard_a").await.unwrap();

    wait_until_killed(&live_arc, "必须 kill 宿主进程").await;
    assert!(invoker.mcp_clients.read().get(&host_key).is_none());
    assert!(invoker.member_last_used.read().get("guard_a").is_none());
    assert!(
        invoker
            .light_packing
            .read()
            .assignments
            .contains_key("guard_a"),
        "热重载语境保留分配表（respawn 按表重建）"
    );
    // 下次调用仍路由到原宿主（分配粘性）
    assert_eq!(invoker.resolve_host_key(&manifest_a), host_key);
}
#[tokio::test]
async fn test_unload_if_idle_light_reclaims_assignments() {
    // idle 语境回收（unload_host reclaim=true）对 light 成员：整组回收 + 槽位释放
    // （分配表清空）——与 idle GC（run_idle_gc_pass）同一调用形态。
    let loader = Arc::new(MockLoader::new());
    let manifest_a = make_light_manifest("guard_a", "python server.py");
    loader.add_manifest(manifest_a.clone());
    let invoker = PluginInvokerImpl::new(loader);

    let host_key = invoker.resolve_host_key(&manifest_a);
    let live = spawn_long_lived_stdio_client().await;
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));

    invoker
        .unload_host(&host_key, true)
        .await
        .expect("sidecar 插件 idle 卸载应成功");
    wait_until_killed(&live_arc, "idle unload must kill host process").await;
    assert!(
        invoker.light_packing.read().assignments.is_empty(),
        "idle 语境回收即清空分配表（槽位释放）"
    );
}
#[tokio::test]
async fn test_existing_host_key_for_light_unassigned_is_none() {
    // 宿主键反查：light 未分配（从未 spawn）→ None；分配后 → 组键；
    // 独占 sidecar → plugin:{id}；InProcess → None；无 manifest → 独占兜底。
    let loader = Arc::new(MockLoader::new());
    let light = make_light_manifest("guard_a", "python server.py");
    let solo = make_sidecar_manifest("solo_tool", "python server.py");
    let native = make_inprocess_manifest("native_plug");
    loader.add_manifest(light.clone());
    loader.add_manifest(solo);
    loader.add_manifest(native);
    let invoker = PluginInvokerImpl::new(loader);

    assert_eq!(
        invoker.existing_host_key_for("guard_a"),
        None,
        "light 从未分配 = 从未 spawn，无宿主"
    );
    let host_key = invoker.resolve_host_key(&light);
    assert_eq!(
        invoker.existing_host_key_for("guard_a"),
        Some(host_key.clone())
    );
    assert_eq!(
        invoker.existing_host_key_for("solo_tool"),
        Some("plugin:solo_tool".to_string())
    );
    assert_eq!(invoker.existing_host_key_for("native_plug"), None);
    assert_eq!(
        invoker.existing_host_key_for("ghost"),
        Some("plugin:ghost".to_string()),
        "无 manifest 时保守按独占兜底（命中缓存即生效）"
    );
}
/// warmup_sidecar：native（InProcess）无进程模型 → no-op Ok，不触发加载/缓存。
#[tokio::test]
async fn warmup_sidecar_native_is_noop() {
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader.clone());
    let mut m = make_sidecar_manifest("native_x", "server.py");
    m.host_type = HostType::InProcess;
    invoker
        .warmup_sidecar(&m)
        .await
        .expect("native 预热 no-op Ok");
    assert!(
        invoker.mcp_clients.read().is_empty(),
        "no-op 不应产生宿主缓存"
    );
}
/// warmup_sidecar：宿主已缓存存活 → 幂等命中（复用同一实例，不 kill 不 respawn）。
/// boot 预热与首条消息并发场景的行为锚点：single-flight/缓存路径与正常调用同源。
#[tokio::test]
async fn warmup_sidecar_reuses_cached_host_without_respawn() {
    let loader = Arc::new(MockLoader::new());
    let manifest = make_sidecar_manifest("warm_hot", "python server.py");
    loader.add_manifest(manifest.clone());
    let invoker = PluginInvokerImpl::new(loader);

    let live = spawn_long_lived_stdio_client().await;
    let arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert("plugin:warm_hot".to_string(), Arc::clone(&arc));

    invoker
        .warmup_sidecar(&manifest)
        .await
        .expect("缓存命中应 Ok");
    // 性质断言：同一宿主实例被复用（缓存仍是原 Arc）且进程仍活（未 kill）
    let still = invoker
        .mcp_clients
        .read()
        .get("plugin:warm_hot")
        .cloned()
        .expect("幂等命中后缓存条目保留");
    assert!(Arc::ptr_eq(&still, &arc), "幂等命中不得 respawn 换实例");
    assert!(
        arc.read().await.is_alive().await,
        "复用路径不得 kill 存活宿主"
    );
}
/// warmup_sidecar：宿主构建失败（无插件目录 → 解释器缺失）→ Err 传播不吞
/// （调用方据此记日志跳过，懒 spawn 仍是运行期兜底）。
#[tokio::test]
async fn warmup_sidecar_spawn_failure_propagates() {
    let loader = Arc::new(MockLoader::new());
    let manifest = make_sidecar_manifest("warm_missing_venv", "python server.py");
    loader.add_manifest(manifest.clone());
    let invoker = PluginInvokerImpl::new(loader);
    let result = invoker.warmup_sidecar(&manifest).await;
    assert!(
        result.is_err(),
        "venv 缺失的失败必须传播（不吞、不假成功）: {result:?}"
    );
    assert!(
        invoker.mcp_clients.read().is_empty(),
        "失败后不得残留缓存条目"
    );
}
/// preassign_host_key：批量预分配定型装箱——合宿组宿主一次 spawn 即装载完整
/// 成员集的前提。solo 成员不写分配表（纯键构造无状态）。
#[tokio::test]
async fn preassign_host_key_batches_light_packing_before_spawn() {
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);

    // 批量预分配按当前生效上限（默认或 AGENTOS_LIGHT_HOST_MAX_MEMBERS）定型装箱：
    // 前 cap 个填满 slot 1，第 cap+1 个溢出 slot 2。容量从生产同源函数推导，
    // 不锚字面量（默认随合宿扩容批次调整，6→10 漏改本测试即红）。
    let cap = light_host_max_members();
    let members: Vec<_> = (0..cap + 1)
        .map(|i| make_light_manifest(&format!("m{i}"), "python server.py"))
        .collect();
    for m in &members {
        invoker.preassign_host_key(m);
    }
    let assignments = invoker.light_packing.read();
    let slot1 = assignments
        .assignments
        .values()
        .filter(|hk| hk.as_str() == "group:light:1")
        .count();
    let slot2 = assignments
        .assignments
        .values()
        .filter(|hk| hk.as_str() == "group:light:2")
        .count();
    assert_eq!(
        slot1, cap,
        "预分配定型：slot 1 装满 cap 成员（非边 spawn 边分配的单成员）"
    );
    assert_eq!(slot2, 1, "溢出成员开 slot 2");
    drop(assignments);

    // 幂等：重复预分配不改归属（粘性）
    for m in &members {
        invoker.preassign_host_key(m);
    }
    assert_eq!(
        invoker.host_members("group:light:1").len(),
        cap,
        "重复预分配不改分配表"
    );

    // solo 成员预分配：纯键构造，不写分配表
    let solo = make_sidecar_manifest("solo_x", "python server.py");
    invoker.preassign_host_key(&solo);
    assert!(
        !invoker
            .light_packing
            .read()
            .assignments
            .contains_key("solo_x"),
        "solo 不进 light 分配表"
    );
}
#[tokio::test]
async fn test_idle_gc_exempts_keep_warm_host_group() {
    // 预热常驻语义：warmup_sidecar 登记的插件所在宿主组不被 idle GC 回收
    // （否则预热被架空，新会话首条消息重新全价冷启动）；非预热宿主照常回收。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_light_manifest("warm_a", "python server.py"));
    loader.add_manifest(make_sidecar_manifest("lazy_b", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);

    // 两组已 spawn 宿主：slot1（light 组，成员 warm_a）与 solo（lazy_b）
    let warm_host = invoker.resolve_host_key(&make_light_manifest("warm_a", "python server.py"));
    assert_eq!(warm_host, "group:light:1");
    let live_warm = Arc::new(tokio::sync::RwLock::new(
        spawn_long_lived_stdio_client().await,
    ));
    invoker
        .mcp_clients
        .write()
        .insert(warm_host.clone(), live_warm.clone());
    // 夹具补全：生产路径 spawn 快照写入先于客户端入缓存（缺快照 = 成员集
    // 未知，BUG-44 修复后按漂移处理整宿主重建，预热测试需遵守同一不变量）
    invoker
        .spawned_members
        .write()
        .insert(warm_host.clone(), invoker.host_members(&warm_host));
    invoker.touch_member_last_used("warm_a");

    let lazy_host = invoker.resolve_host_key(&make_sidecar_manifest("lazy_b", "python server.py"));
    assert_eq!(lazy_host, "plugin:lazy_b");
    let live_lazy = Arc::new(tokio::sync::RwLock::new(
        spawn_long_lived_stdio_client().await,
    ));
    invoker
        .mcp_clients
        .write()
        .insert(lazy_host.clone(), live_lazy.clone());
    invoker.touch_member_last_used("lazy_b");

    // warm_a 登记预热常驻（公开入口）：MockLoader 无真实目录 → spawn 失败
    // 返回 Err，但常驻登记照常生效（warmup_sidecar 契约：失败跳过、登记不变）
    let _ = invoker
        .warmup_sidecar(&make_light_manifest("warm_a", "python server.py"))
        .await;

    // 两个成员都置为空闲 400s（> 默认阈值 300s；记账键 = plugin_id）
    for pid in ["warm_a", "lazy_b"] {
        invoker
            .member_last_used
            .write()
            .insert(pid.to_string(), Instant::now() - Duration::from_secs(400));
    }

    invoker.run_idle_gc_pass().await;

    assert!(
        live_warm.read().await.is_alive().await,
        "预热常驻宿主组不得被 idle GC 回收"
    );
    wait_until_killed(&live_lazy, "非预热宿主组照常整组回收").await;
    assert!(
        invoker.mcp_clients.read().contains_key(&warm_host),
        "预热宿主缓存保留（下次调用零冷启动）"
    );
    assert!(
        !invoker.mcp_clients.read().contains_key(&lazy_host),
        "回收宿主缓存清空"
    );
}
#[tokio::test]
async fn test_explicit_unload_still_works_on_keep_warm_host() {
    // 豁免只约束 idle GC 的自动回收；显式卸载（force_unload）不受预热常驻约束。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_light_manifest("warm_c", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);

    let host = invoker.resolve_host_key(&make_light_manifest("warm_c", "python server.py"));
    let live = Arc::new(tokio::sync::RwLock::new(
        spawn_long_lived_stdio_client().await,
    ));
    invoker
        .mcp_clients
        .write()
        .insert(host.clone(), live.clone());
    // 夹具补全（同 test_idle_gc_exempts_keep_warm_host_group）：spawn 快照
    // 先于缓存写入的生产不变量，warmup 才能走「复用」而非「未知成员集重建」
    invoker
        .spawned_members
        .write()
        .insert(host.clone(), invoker.host_members(&host));
    invoker.touch_member_last_used("warm_c");
    let _ = invoker
        .warmup_sidecar(&make_light_manifest("warm_c", "python server.py"))
        .await;

    invoker
        .force_unload_impl("warm_c")
        .await
        .expect("显式卸载不受预热常驻豁免约束");
    wait_until_killed(&live, "显式卸载后子进程退出").await;
}
#[tokio::test]
async fn host_proc_snapshots_empty_invoker_is_empty() {
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    assert!(invoker.host_proc_snapshots().await.is_empty());
}
#[tokio::test]
async fn host_proc_snapshots_reports_member_uptime_and_alive() {
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    invoker
        .mcp_clients
        .write()
        .insert("plugin:demo".to_string(), unconnected_stdio_client());
    invoker
        .host_spawned_at
        .write()
        .insert("plugin:demo".to_string(), Instant::now());

    let snaps = invoker.host_proc_snapshots().await;
    assert_eq!(snaps.len(), 1);
    let s = &snaps[0];
    assert_eq!(s.host_key, "plugin:demo");
    assert!(
        !s.alive,
        "缓存中无 child 的 stdio client = 未连接/已 kill 残留，应判死"
    );
    assert_eq!(s.pid, None);
    assert!(s.uptime_secs.is_some(), "有 spawn 记账应有 uptime");
    // 独占宿主：成员 = 宿主键前缀解析
    assert_eq!(s.plugin_ids, vec!["demo".to_string()]);
}
#[tokio::test]
async fn host_proc_snapshots_light_group_lists_all_members() {
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    invoker
        .mcp_clients
        .write()
        .insert("group:light:1".to_string(), unconnected_stdio_client());
    // 轻量组成员 = packing 分配表（plugin_id → 宿主键）
    invoker
        .light_packing
        .write()
        .assignments
        .insert("member_a".to_string(), "group:light:1".to_string());
    invoker
        .light_packing
        .write()
        .assignments
        .insert("member_b".to_string(), "group:light:1".to_string());

    let snaps = invoker.host_proc_snapshots().await;
    assert_eq!(snaps.len(), 1);
    let mut ids = snaps[0].plugin_ids.clone();
    ids.sort();
    assert_eq!(ids, vec!["member_a".to_string(), "member_b".to_string()]);
}
#[tokio::test]
async fn host_proc_snapshots_without_spawn_record_omits_uptime() {
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    invoker
        .mcp_clients
        .write()
        .insert("plugin:x".to_string(), unconnected_stdio_client());
    let snaps = invoker.host_proc_snapshots().await;
    assert_eq!(snaps.len(), 1);
    assert_eq!(snaps[0].uptime_secs, None, "无 spawn 记账 uptime 应为 None");
}
#[tokio::test]
async fn emit_lifecycle_error_fans_out_to_bus_with_context() {
    // OnError 旁路广播契约：emit 后总线订阅者收到事件，ctx 携带 plugin_id /
    // error / error_code（监控页"插件报错"计数的数据源）。
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    let bus = Arc::new(HookEventBus::new(32));
    invoker.set_hook_bus(bus.clone());
    let mut rx = bus.subscribe();

    invoker.emit_lifecycle_error(
        "my_plugin",
        "tool execution failed",
        Some("MCP_TOOL_CALL_FAILED"),
    );

    let ev = tokio::time::timeout(std::time::Duration::from_millis(500), rx.recv())
        .await
        .expect("应在超时前收到 OnError 事件")
        .expect("总线未关闭");
    assert_eq!(ev.hook, LifecycleHook::OnError);
    assert_eq!(ev.ctx.get("plugin_id"), Some(&json!("my_plugin")));
    assert_eq!(ev.ctx.get("error"), Some(&json!("tool execution failed")));
    assert_eq!(
        ev.ctx.get("error_code"),
        Some(&json!("MCP_TOOL_CALL_FAILED"))
    );
}
#[tokio::test]
async fn emit_lifecycle_error_without_bus_is_noop() {
    // bus 未注入（如单测环境）时 no-op 不 panic——best-effort 观察路径契约。
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    invoker.emit_lifecycle_error("my_plugin", "boom", None);
}
#[test]
fn pipeline_error_params_counts_err_and_ok_error_field() {
    // Err（崩溃/MCP/解析）→ 报错；Ok 携带 error 字段（业务失败）→ 同样报错；
    // Ok 无 error → None 不发事件。
    let err = Err(PluginError {
        message: "plugin process crashed: p".to_string(),
        code: Some("PLUGIN_CRASHED".to_string()),
        source: None,
    });
    let (msg, code) = pipeline_error_params(&err).expect("Err 应计报错");
    assert_eq!(msg, "plugin process crashed: p");
    assert_eq!(code.as_deref(), Some("PLUGIN_CRASHED"));

    let ok_err = Ok(PluginResult::default().with_error(PluginError {
        message: "step blew up".to_string(),
        code: None,
        source: None,
    }));
    let (msg, code) = pipeline_error_params(&ok_err).expect("Ok error 字段应计报错");
    assert_eq!(msg, "step blew up");
    assert_eq!(code, None);

    assert!(pipeline_error_params(&Ok(PluginResult::default())).is_none());
}
#[test]
fn tool_error_params_counts_success_false_only() {
    // success=false（isError/失败信封）→ 报错，error 缺省给兜底文案；
    // success=true → None。两个失败形态是计数器的行为契约。
    let failed = Ok(ToolExecutionResult::failure("container_create_failed"));
    let (msg, code) = tool_error_params(&failed).expect("success=false 应计报错");
    assert_eq!(msg, "container_create_failed");
    assert_eq!(code, None);

    let failed_no_msg = Ok(ToolExecutionResult {
        success: false,
        data: json!({}),
        error: None,
        duration_ms: None,
        metadata: None,
    });
    let (msg, _) = tool_error_params(&failed_no_msg).expect("success=false 无 error 也计");
    assert_eq!(msg, "tool execution failed");

    assert!(tool_error_params(&Ok(ToolExecutionResult::success(json!({})))).is_none());
    assert!(tool_error_params(&Err(PluginError {
        message: "MCP tool call failed".to_string(),
        code: Some("MCP_TOOL_CALL_FAILED".to_string()),
        source: None,
    }))
    .is_some());
}
#[test]
fn test_scoped_router_identity_light_member_uses_host_key() {
    // 合宿成员身份 = 宿主键（进程即主体），不随组重生锚定成员漂移。
    let m = make_light_manifest("pipeline_tool_schema", "python server.py");
    assert_eq!(
        super::scoped_router_identity(&m, "group:light:3"),
        "group:light:3"
    );
}
#[test]
fn test_scoped_router_identity_exclusive_uses_manifest_id() {
    // 独占插件身份 = manifest.id（现状语义；注意不是 plugin: 前缀宿主键）。
    let m = make_sidecar_manifest("llm_core", "python server.py");
    assert_eq!(
        super::scoped_router_identity(&m, "plugin:llm_core"),
        "llm_core"
    );
}
#[test]
fn test_scoped_router_identity_external_mcp_not_grouped() {
    // host_group=light 但外部 stdio 命令 → 不进合宿，身份仍 = manifest.id。
    let mut m = make_light_manifest("external_stdio", "unused entry");
    m.mcp = Some(McpConfig {
        allowed_roots: Vec::new(),
        transport: McpTransport::Stdio,
        endpoint: Some(McpEndpoint {
            command: Some("npx".to_string()),
            args: vec!["-y".to_string()],
            ..Default::default()
        }),
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: None,
    });
    assert_eq!(
        super::scoped_router_identity(&m, "group:light:1"),
        "external_stdio"
    );
}
#[tokio::test]
async fn test_run_freeze_dump_writes_report_with_fake_spy() {
    // 冻结取证契约：假 py-spy 替身（外部进程是可替身依赖）——产物落盘、
    // 文件名带 plugin/pid、正文含元信息头与替身 stdout。
    let dir = tempfile::tempdir().unwrap();
    let fake = dir.path().join(if cfg!(windows) {
        "fake-spy.cmd"
    } else {
        "fake-spy"
    });
    if cfg!(windows) {
        std::fs::write(&fake, "@echo off\r\necho Thread 0x1 (idle): run_loop\r\n").unwrap();
    } else {
        std::fs::write(&fake, "#!/bin/sh\necho 'Thread 0x1 (idle): run_loop'\n").unwrap();
        // unix 侧替身必须可执行：execve 无执行位恒 EACCES（Linux CI 实锤，
        // 09-05 落地起潜伏必挂，被 cargo fail-fast 的前序失败掩蔽至今）。
        // set_mode 惯用法与 integration-tests/config_injection_test.rs 同款
        //（Windows 侧 cfg(unix) 剥离，本地 clippy 不查 unix 分支，CI 执法）。
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perms = std::fs::metadata(&fake).unwrap().permissions();
            perms.set_mode(0o755);
            std::fs::set_permissions(&fake, perms).unwrap();
        }
    }
    let out_dir = dir.path().join("dumps");
    let got = PluginInvokerImpl::run_freeze_dump(
        fake.to_str().unwrap(),
        "llm_service",
        4242,
        42,
        &out_dir,
        10,
    )
    .await;
    let path = got.expect("假 spy 成功应返回产物路径");
    let name = path.file_name().unwrap().to_string_lossy().to_string();
    assert!(
        name.starts_with("llm_service_") && name.ends_with("_pid4242.txt"),
        "产物名: {name}"
    );
    let body = std::fs::read_to_string(&path).unwrap();
    assert!(body.contains("# plugin: llm_service"), "元信息头缺失");
    assert!(body.contains("# pid: 4242"));
    assert!(body.contains("# heartbeat_stale_secs: 42"));
    assert!(body.contains("run_loop"), "应包含替身 py-spy 的 stdout");
}
#[tokio::test]
async fn test_run_freeze_dump_missing_spy_degrades_to_none() {
    // 尽力而为契约：spy 二进制不存在 → None（降级告警），不 panic 不长阻塞，
    // 调用点随后照常 force_unload 自愈。
    let dir = tempfile::tempdir().unwrap();
    let got = PluginInvokerImpl::run_freeze_dump(
        "no-such-py-spy-binary-xyz",
        "llm_service",
        1,
        31,
        dir.path(),
        10,
    )
    .await;
    assert!(got.is_none(), "spy 缺失应降级返回 None");
}
#[tokio::test]
async fn test_run_freeze_dump_timeout_degrades_to_none() {
    // 挂死 spy（超过 timeout 不退出）→ None 降级，不留产物（回收硬时限优先）。
    let dir = tempfile::tempdir().unwrap();
    let slow = dir.path().join(if cfg!(windows) {
        "slow-spy.cmd"
    } else {
        "slow-spy"
    });
    if cfg!(windows) {
        std::fs::write(&slow, "@echo off\r\nping -n 30 127.0.0.1 >nul\r\n").unwrap();
    } else {
        std::fs::write(&slow, "#!/bin/sh\nsleep 30\n").unwrap();
        // 同 fake 替身：无执行位在 unix 恒 EACCES——超时路径会退化成启动
        // 失败路径"碰巧绿"，chmod 后才真测挂死超时。
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perms = std::fs::metadata(&slow).unwrap().permissions();
            perms.set_mode(0o755);
            std::fs::set_permissions(&slow, perms).unwrap();
        }
    }
    let got = PluginInvokerImpl::run_freeze_dump(
        slow.to_str().unwrap(),
        "llm_service",
        7,
        35,
        dir.path(),
        1,
    )
    .await;
    assert!(got.is_none(), "超时应降级返回 None");
    let leftover: Vec<_> = std::fs::read_dir(dir.path())
        .unwrap()
        .filter_map(|e| e.ok())
        .map(|e| e.file_name().to_string_lossy().to_string())
        .filter(|n| n.contains("llm_service"))
        .collect();
    assert!(leftover.is_empty(), "超时不得残留半截产物: {leftover:?}");
}
#[tokio::test]
async fn test_p12_idle_gc_skips_host_with_inflight_calls() {
    // in-flight>0 且空闲超期 → 不回收（宿主键条目保留 = unload_host 未执行）
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_p12_manifest("p12a", 300));
    let invoker = PluginInvokerImpl::new(loader);
    let host_key = "plugin:p12a";
    backdate_idle(&invoker, "p12a", 400);

    let _guard = invoker.enter_inflight(host_key);
    assert_eq!(invoker.host_inflight(host_key), 1);

    invoker.run_idle_gc_pass().await;

    assert!(
        invoker.member_last_used.read().contains_key("p12a"),
        "in-flight>0 时超阈值成员不得被回收"
    );
}
#[tokio::test]
async fn test_p12_idle_gc_unloads_host_after_calls_settle() {
    // 同宿主计数归零后，超阈值 → 正常回收（last_used 条目被 unload_host 清除）
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_p12_manifest("p12b", 300));
    let invoker = PluginInvokerImpl::new(loader);
    let host_key = "plugin:p12b";
    backdate_idle(&invoker, "p12b", 400);

    {
        let _guard = invoker.enter_inflight(host_key);
        assert_eq!(invoker.host_inflight(host_key), 1);
    } // guard drop → 计数回落
    assert_eq!(invoker.host_inflight(host_key), 0, "调用结束计数必须归零");

    invoker.run_idle_gc_pass().await;

    assert!(
        !invoker.member_last_used.read().contains_key("p12b"),
        "计数归零后超阈值成员应被正常回收"
    );
    assert_eq!(
        invoker.host_inflight(host_key),
        0,
        "回收清理后计数条目不残留"
    );
}
#[tokio::test]
async fn idle_gc_bypasses_residual_inflight_pin_after_ceiling() {
    // 大幅回拨环境前置（见 uptime_supports_backdate）：开机不足 40_000s 跳过
    if !uptime_supports_backdate(40_000) {
        return;
    }
    // F1（R7 移动版台账）：轮后残留 in-flight guard 可把 idle GC 无限推迟
    // （残留 guard 存活期 = idle GC 失效期）。兜底语义：宿主全部在飞 guard
    // 创建时刻超残留上限（缺省 1200s）且无一属持久保活交互族 → 判残留 pin，
    // GC 不再等待、照常回收。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_p12_manifest("p12res", 300));
    let invoker = PluginInvokerImpl::new(loader.clone());
    let manifest = loader.get_manifest("p12res").expect("manifest 在册");
    let (host_key, _guard, _member_guard) = invoker.enter_sidecar_inflight(&manifest);
    backdate_idle(&invoker, "p12res", 400);
    // 伪造残留：guard 创建时刻回拨远超上限（生产签名 = 计数在悬、无活跃轮在案）
    backdate_inflight_since(&invoker, &host_key, 40_000);

    invoker.run_idle_gc_pass().await;

    assert!(
        !invoker.member_last_used.read().contains_key("p12res"),
        "全部 guard 超残留上限时不得继续推迟回收（残留 pin 兜底豁免）"
    );
}
#[tokio::test]
async fn inflight_pin_residual_judgment_contract() {
    // 大幅回拨环境前置（见 uptime_supports_backdate）：开机不足 40_000s 跳过
    if !uptime_supports_backdate(40_000) {
        return;
    }
    // 残留判定契约（F1 兜底的判定面）：年轻 guard 不豁免（正常长任务红线）；
    // 持久保活交互族（idle_timeout_secs=0，wait_for_choice 停泊）无论多老不
    // 豁免；空记账保守不豁免；混有保活 guard 不豁免；全员超龄非保活才豁免。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_p12_manifest("p12n", 300));
    loader.add_manifest(make_p12_manifest("p12k", 0));
    let invoker = PluginInvokerImpl::new(loader.clone());
    let m_n = loader.get_manifest("p12n").unwrap();
    let m_k = loader.get_manifest("p12k").unwrap();
    let (hk_n, _g_n, _mg_n) = invoker.enter_sidecar_inflight(&m_n);
    let (hk_k, _g_k, _mg_k) = invoker.enter_sidecar_inflight(&m_k);

    assert!(
        !invoker.host_inflight_pin_is_residual(&hk_n),
        "年轻 guard = 正常调用在飞，不得判残留（长任务红线）"
    );
    assert!(
        !invoker.host_inflight_pin_is_residual("plugin:absent"),
        "无记账（registry 缺席）保守不豁免"
    );

    backdate_inflight_since(&invoker, &hk_n, 40_000);
    backdate_inflight_since(&invoker, &hk_k, 40_000);
    assert!(
        invoker.host_inflight_pin_is_residual(&hk_n),
        "全员超龄且非保活 → 残留"
    );
    assert!(
        !invoker.host_inflight_pin_is_residual(&hk_k),
        "保活交互族 guard 超龄也不得判残留（停泊红线）"
    );

    // 混合宿主：超龄非保活 + 超龄保活并存 → 不豁免（保活钉全组，ADR 2026-09-28）
    if let Some(registry) = invoker.inflight_since.read().get(&hk_n).cloned() {
        registry.lock().insert(
            9_999,
            (
                Instant::now() - Duration::from_secs(40_000),
                "p12k".to_string(),
            ),
        );
    }
    assert!(
        !invoker.host_inflight_pin_is_residual(&hk_n),
        "混有保活成员 guard 时不得豁免"
    );
}
#[tokio::test]
async fn idle_gc_soft_member_unload_bypasses_residual_pin_but_keeps_alive_pin() {
    // 大幅回拨环境前置（见 uptime_supports_backdate）：开机不足 40_000s 跳过
    if !uptime_supports_backdate(40_000) {
        return;
    }
    // 成员粒度软卸载分支的同门（F1）：保活交互族 guard 超龄仍钉住同组（红线，
    // 一个钉子冻全组是 ADR 2026-09-28 的刻意语义）；仅剩超龄非保活 guard 时
    // 豁免放行软卸载。真宿主端到端（与在飞门测试同款跳过守卫）。
    let ok = std::process::Command::new(python_exe())
        .arg("-c")
        .arg("import agentos_plugin_sdk, mcp")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    if !ok {
        eprintln!("python 或 agentos_plugin_sdk 不可用，跳过 GC 残留 pin 端到端");
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
    // b_service 声明持久保活（交互族：模拟 human_interaction 停泊等待）
    let mut m_b = make_light_manifest("b_service", "python server.py");
    m_b.lifecycle = Some(PluginLifecycle {
        idle_timeout_secs: Some(0),
    });
    loader.add_manifest(m_b);
    let invoker = PluginInvokerImpl::new(loader.clone());
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

    let manifest_a = loader.get_manifest("a_service").unwrap();
    let manifest_b = loader.get_manifest("b_service").unwrap();
    let (_hk, guard_a, _mga) = invoker.enter_sidecar_inflight(&manifest_a);
    let (_hk2, guard_b, _mgb) = invoker.enter_sidecar_inflight(&manifest_b);
    assert_eq!(_hk, host_key, "前置：a_service 落在组宿主");
    assert_eq!(_hk2, host_key, "前置：b_service 落在同一组宿主");
    backdate_idle(&invoker, "a_service", 40_000);
    backdate_inflight_since(&invoker, &host_key, 40_000);

    invoker.run_idle_gc_pass().await;
    assert!(
        invoker.member_last_used.read().contains_key("a_service"),
        "保活成员 guard 超龄仍钉住同组：空闲成员不得软卸载（红线）"
    );

    // 保活 guard 释放后，仅剩超龄非保活 guard → 豁免放行软卸载
    drop(guard_b);
    invoker.run_idle_gc_pass().await;
    assert!(
        !invoker.member_last_used.read().contains_key("a_service"),
        "仅剩超龄非保活 guard → 残留豁免放行软卸载（F1 兜底）"
    );
    assert_eq!(
        invoker.spawned_members.read().get(&host_key).cloned(),
        Some(vec!["b_service".to_string()]),
        "软卸载只摘超期成员，保活成员服务面不动"
    );
    drop(guard_a);
    invoker.shutdown_all().await;
}
#[tokio::test]
#[allow(clippy::await_holding_lock)] // 刻意跨 await：串行锁护住进程级 env 注入全程
async fn pool_keepwarm_exempt_idle_host_within_quota() {
    // 回拨量级说明：本节用例阈值与回拨都在秒级（manifest 阈值 1s + 回拨 2s），
    // 不需要 uptime_supports_backdate 护栏——能跑 cargo test 的机器开机时长
    // 必然 > 3s（R3 的 40_000s 护栏针对 11 小时量级，不同量级）；且无守卫
    // 分支 = 全行恒执行，不落 llvm-cov 恒 0 命中的独行 return（R7 教训同族）。
    let _serial = ENV_SENSITIVE_LOCK.lock();
    let _env = EnvVarGuard::set("AGENTOS_HOST_POOL_KEEPWARM", "1");
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_p12_manifest("poola", 1));
    let invoker = PluginInvokerImpl::new(loader);
    let host_key = pool_solo_host(&invoker, "poola", 0);
    // 空闲超阈值（1s）：旧行为本轮必回收
    backdate_idle(&invoker, "poola", 2);

    invoker.run_idle_gc_pass().await;

    // 配额内空闲宿主免杀：成员记账保留（未被回收）
    assert!(invoker.member_last_used.read().contains_key("poola"));
    // 配额内空闲宿主免杀：进程缓存条目保留（下次调用 fast path 直接复用）
    assert!(invoker.mcp_clients.read().contains_key(&host_key));
    invoker.shutdown_all().await;
}
#[tokio::test]
#[allow(clippy::await_holding_lock)] // 刻意跨 await：串行锁护住进程级 env 注入全程
async fn pool_keepwarm_lru_evicts_host_beyond_quota() {
    let _serial = ENV_SENSITIVE_LOCK.lock();
    let _env = EnvVarGuard::set("AGENTOS_HOST_POOL_KEEPWARM", "1");
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_p12_manifest("poolhot", 1));
    loader.add_manifest(make_p12_manifest("poolcold", 1));
    loader.add_manifest(make_p12_manifest("poolghost", 1));
    let invoker = PluginInvokerImpl::new(loader);
    let hk_hot = pool_solo_host(&invoker, "poolhot", 0);
    let hk_cold = pool_solo_host(&invoker, "poolcold", 0);
    // ghost：健康在册但无任何成员使用记账（LRU 热度垫底分支），GC 循环不访问
    let _hk_ghost = pool_solo_host(&invoker, "poolghost", 0);
    // LRU 判别只需两档不同热度（2s < 3s，均超 1s 阈值）
    backdate_idle(&invoker, "poolhot", 2);
    backdate_idle(&invoker, "poolcold", 3);

    invoker.run_idle_gc_pass().await;

    // 最热宿主占配额免杀
    assert!(invoker.mcp_clients.read().contains_key(&hk_hot));
    // 免杀宿主成员记账保留
    assert!(invoker.member_last_used.read().contains_key("poolhot"));
    // 超配额按 LRU 淘汰：最冷宿主照旧回收
    assert!(!invoker.mcp_clients.read().contains_key(&hk_cold));
    // 被淘汰宿主成员记账随回收清理
    assert!(!invoker.member_last_used.read().contains_key("poolcold"));
    invoker.shutdown_all().await;
}
#[tokio::test]
#[allow(clippy::await_holding_lock)] // 刻意跨 await：串行锁护住进程级 env 注入全程
async fn pool_ttl_hard_cap_reclaims_host_despite_quota() {
    let _serial = ENV_SENSITIVE_LOCK.lock();
    // 配额 4 ≥ 候选 2：TTL 是唯一判别维度；TTL 收紧到 2s 配回拨 3s
    let _env_keep = EnvVarGuard::set("AGENTOS_HOST_POOL_KEEPWARM", "4");
    let _env_ttl = EnvVarGuard::set("AGENTOS_HOST_POOL_TTL_SECS", "2");
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_p12_manifest("pooldue", 1));
    loader.add_manifest(make_p12_manifest("poolyoung", 1));
    let invoker = PluginInvokerImpl::new(loader);
    let hk_young = pool_solo_host(&invoker, "poolyoung", 0);
    let hk_due = pool_solo_host(&invoker, "pooldue", 0);
    // 旧宿主 spawn 记账回拨 3s（> TTL 2s）：双保险硬顶生效
    let old = Instant::now() - Duration::from_secs(3);
    invoker.host_spawned_at.write().insert(hk_due.clone(), old);
    for pid in ["pooldue", "poolyoung"] {
        backdate_idle(&invoker, pid, 2);
    }

    invoker.run_idle_gc_pass().await;

    // TTL 到顶宿主配额内也照旧回收（硬顶，防长跑慢性膨胀）
    assert!(!invoker.mcp_clients.read().contains_key(&hk_due));
    // TTL 到顶宿主成员记账随回收清理
    assert!(!invoker.member_last_used.read().contains_key("pooldue"));
    // TTL 未到顶宿主照常免杀
    assert!(invoker.mcp_clients.read().contains_key(&hk_young));
    invoker.shutdown_all().await;
}
#[tokio::test]
#[allow(clippy::await_holding_lock)] // 刻意跨 await：串行锁护住进程级 env 注入全程
async fn pool_max_requests_rotation_marks_reclaim_candidate() {
    let _serial = ENV_SENSITIVE_LOCK.lock();
    let _env_keep = EnvVarGuard::set("AGENTOS_HOST_POOL_KEEPWARM", "4");
    let _env_max = EnvVarGuard::set("AGENTOS_HOST_POOL_MAX_REQUESTS", "5");
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_p12_manifest("poolover", 1));
    loader.add_manifest(make_p12_manifest("poolatlim", 1));
    let invoker = PluginInvokerImpl::new(loader);
    // 超限（6 > 5）= 轮换候选；恰在上限（5，严格大于才超限）= 正常保活
    let hk_over = pool_solo_host(&invoker, "poolover", 6);
    let hk_at = pool_solo_host(&invoker, "poolatlim", 5);
    backdate_idle(&invoker, "poolover", 2);
    backdate_idle(&invoker, "poolatlim", 2);

    invoker.run_idle_gc_pass().await;

    // 累计请求超限宿主标记轮换候选：保活豁免不再生效，照旧回收
    assert!(!invoker.mcp_clients.read().contains_key(&hk_over));
    // 轮换候选回收后成员记账清理（respawn 由池补位或照旧懒拉起）
    assert!(!invoker.member_last_used.read().contains_key("poolover"));
    // 计数恰在上限不构成轮换候选（超限=严格大于），照常保活
    assert!(invoker.mcp_clients.read().contains_key(&hk_at));
    invoker.shutdown_all().await;
}
#[tokio::test]
#[allow(clippy::await_holding_lock)] // 刻意跨 await：串行锁护住进程级 env 注入全程
async fn pool_keepwarm_skips_unhealthy_client_legacy_cleanup() {
    let _serial = ENV_SENSITIVE_LOCK.lock();
    let _env = EnvVarGuard::set("AGENTOS_HOST_POOL_KEEPWARM", "2");
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_p12_manifest("pooldead", 1));
    let invoker = PluginInvokerImpl::new(loader);
    // 死进程替身：无 child 的 stdio client（判死，与 monitoring 测试同款构造）
    let host_key = "plugin:pooldead".to_string();
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), unconnected_stdio_client());
    invoker
        .host_spawned_at
        .write()
        .insert(host_key.clone(), Instant::now());
    backdate_idle(&invoker, "pooldead", 2);

    invoker.run_idle_gc_pass().await;

    // 半死/死进程不豁免：保活免杀只对缓存里有健康客户端的宿主生效
    assert!(!invoker.mcp_clients.read().contains_key(&host_key));
    // 死进程宿主照旧走既有清理路径
    assert!(!invoker.member_last_used.read().contains_key("pooldead"));
}
#[tokio::test]
#[allow(clippy::await_holding_lock)] // 刻意跨 await：串行锁护住进程级 env 注入全程
async fn pool_disabled_zero_keeps_legacy_idle_reclaim() {
    let _serial = ENV_SENSITIVE_LOCK.lock();
    // 显式 0 = 池关闭 = 旧行为（默认未设同值，解析单测覆盖 None 态）
    let _env = EnvVarGuard::set("AGENTOS_HOST_POOL_KEEPWARM", "0");
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_p12_manifest("pooloff", 1));
    let invoker = PluginInvokerImpl::new(loader);
    let host_key = pool_solo_host(&invoker, "pooloff", 0);
    backdate_idle(&invoker, "pooloff", 2);

    invoker.run_idle_gc_pass().await;

    // 池关闭（keepwarm=0）：空闲超期宿主照旧回收，零行为变化
    assert!(!invoker.mcp_clients.read().contains_key(&host_key));
    // 池关闭：成员记账照旧清理
    assert!(!invoker.member_last_used.read().contains_key("pooloff"));
}
#[tokio::test]
#[allow(clippy::await_holding_lock)] // 刻意跨 await：串行锁护住进程级 env 注入全程
async fn pool_keepwarm_guards_member_soft_unload_path() {
    let _serial = ENV_SENSITIVE_LOCK.lock();
    let _env = EnvVarGuard::set("AGENTOS_HOST_POOL_KEEPWARM", "1");
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_light_manifest("poolm_a", "python server.py"));
    loader.add_manifest(make_light_manifest("poolm_b", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);
    invoker.assign_light_host_with("poolm_a", "light", 6);
    invoker.assign_light_host_with("poolm_b", "light", 6);
    let host_key = "group:light:1".to_string();
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), healthy_http_client());
    invoker
        .spawned_members
        .write()
        .insert(host_key.clone(), invoker.host_members(&host_key));
    invoker
        .host_spawned_at
        .write()
        .insert(host_key.clone(), Instant::now());
    invoker.touch_member_last_used("poolm_a");
    invoker.touch_member_last_used("poolm_b");
    // a 空闲超阈值（1s；b 活跃）：成员粒度路径本会进程内软卸载 a
    backdate_idle(&invoker, "poolm_a", 2);

    invoker.run_idle_gc_pass().await;

    // 池免杀是宿主粒度：成员软卸载分支同样跳过（服务面保持完整，冷启归零）
    assert!(invoker.member_last_used.read().contains_key("poolm_a"));
    // 免杀宿主进程服务面不收缩
    let full_service = Some(vec!["poolm_a".to_string(), "poolm_b".to_string()]);
    assert_eq!(
        invoker.spawned_members.read().get(&host_key).cloned(),
        full_service
    );
    // 免杀宿主进程缓存条目保留
    assert!(invoker.mcp_clients.read().contains_key(&host_key));
    invoker.shutdown_all().await;
}
#[test]
fn test_p12_tool_timeout_ms_precedence() {
    // ① 工具声明生效
    let mut m = make_sidecar_manifest("p12d", "python server.py");
    m.capabilities.tools = vec![p12_tool_cap("t", Some(123_456))];
    m.mcp = Some(McpConfig {
        allowed_roots: Vec::new(),
        transport: McpTransport::Stdio,
        endpoint: None,
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: Some(9),
    });
    assert_eq!(tool_timeout_ms(&m, "t"), Some(123_456), "工具声明值生效");

    // ② 未声明工具 → 插件级 request_timeout_secs 兜底（秒→毫秒）
    let mut m2 = make_sidecar_manifest("p12e", "python server.py");
    m2.capabilities.tools = vec![p12_tool_cap("t", None)];
    m2.mcp = Some(McpConfig {
        allowed_roots: Vec::new(),
        transport: McpTransport::Stdio,
        endpoint: None,
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: Some(1_800),
    });
    assert_eq!(
        tool_timeout_ms(&m2, "t"),
        Some(1_800_000),
        "插件级声明兜底（秒→毫秒）"
    );

    // ③ 全未声明 → 内核默认 300_000ms
    let m3 = make_sidecar_manifest("p12f", "python server.py");
    assert_eq!(
        tool_timeout_ms(&m3, "nope"),
        Some(default_capability_timeout_ms())
    );

    // ④ Some(0) = 显式豁免
    let mut m4 = make_sidecar_manifest("p12g", "python server.py");
    m4.capabilities.tools = vec![p12_tool_cap("t", Some(0))];
    assert_eq!(tool_timeout_ms(&m4, "t"), None, "Some(0) = 豁免总时长超时");

    // ⑤ 按工具名隔离：未命中声明的工具走兜底，不吃别的工具的声明
    let mut m5 = make_sidecar_manifest("p12h", "python server.py");
    m5.capabilities.tools = vec![p12_tool_cap("fast", Some(1_000))];
    assert_eq!(
        tool_timeout_ms(&m5, "other"),
        Some(default_capability_timeout_ms())
    );
}
#[test]
fn test_p12_manifest_timeout_ms_serde_roundtrip() {
    // 声明值经真实 serde 面生效（序列化出口 + 反序列化入口）
    let mut m = make_sidecar_manifest("p12i", "python server.py");
    m.capabilities.tools = vec![p12_tool_cap("t", Some(60_000))];
    let v = serde_json::to_value(&m).unwrap();
    assert_eq!(v["capabilities"]["tools"][0]["timeout_ms"], 60_000);
    let round: PluginManifest = serde_json::from_value(v).unwrap();
    assert_eq!(round.capabilities.tools[0].timeout_ms, Some(60_000));

    // 存量 manifest 无该字段 → serde default 解析为 None（字段只增不删，兼容）
    let mut m2 = make_sidecar_manifest("p12j", "python server.py");
    m2.capabilities.tools = vec![p12_tool_cap("t", None)];
    let v2 = serde_json::to_value(&m2).unwrap();
    assert!(
        v2["capabilities"]["tools"][0].get("timeout_ms").is_none(),
        "None 不序列化（skip_serializing_if）"
    );
    let round2: PluginManifest = serde_json::from_value(v2).unwrap();
    assert_eq!(round2.capabilities.tools[0].timeout_ms, None);
}
#[tokio::test]
async fn test_p12_capability_timeout_none_is_passthrough() {
    // 豁免（None）：不包界——内层 future 正常完成，语义零改变
    let result =
        with_capability_timeout(None, "p12k", "t", async { Ok::<(), PluginError>(()) }).await;
    assert!(result.is_ok(), "豁免路径透传内层结果");
}
#[tokio::test]
async fn test_p22_system_direct_call_blocks_reclaim_at_unified_entry() {
    // System 插件（服务式直调形态，llm_service 同构）调用进行中：统一入口
    // 计数 >0 → 超阈值 GC 走 skipped 路径不回收；计数回落 → 正常回收。
    let loader = Arc::new(MockLoader::new());
    let m = make_p22_llm_shape_manifest("llm_like");
    let host_key = solo_host_key(&m.id);
    loader.add_manifest(m.clone());
    let invoker = PluginInvokerImpl::new(loader);

    // 统一入口计入（与 invoke_pipeline_plugin/invoke_tool Sidecar 分支同代码）
    assert_eq!(tool_timeout_ms(&m, "llm.complete_stream"), Some(3_600_000));
    let (counted_key, guard, _member_guard) = invoker.enter_sidecar_inflight(&m);
    assert_eq!(counted_key, host_key, "System 独占宿主键 = solo_host_key");
    assert_eq!(invoker.host_inflight(&host_key), 1);

    backdate_idle(&invoker, "llm_like", 400);
    invoker.run_idle_gc_pass().await;
    assert!(
        invoker.member_last_used.read().contains_key("llm_like"),
        "流式直调进行中（计数=1）超阈值成员必须走 skipped 路径不回收"
    );

    drop(guard); // 流结束/断连 → RAII 回落
    assert_eq!(invoker.host_inflight(&host_key), 0, "流结束后计数必须归零");
    invoker.run_idle_gc_pass().await;
    assert!(
        !invoker.member_last_used.read().contains_key("llm_like"),
        "计数归零后超阈值成员应被正常回收"
    );
}
#[test]
fn sweep_freeze_dump_dir_keeps_latest_n_deletes_oldest() {
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("freeze_dumps");
    std::fs::create_dir_all(&dir).unwrap();
    // 12 份 dump，写时间逐份 +60s 排开（显式 set_modified，不依赖写盘时戳精度）
    let base = std::time::SystemTime::now();
    for i in 0..12u64 {
        let p = dir.join(format!("pluginA_{:012}_pid1.txt", 1_000_000 + i));
        std::fs::write(&p, "dump").unwrap();
        let f = std::fs::OpenOptions::new().write(true).open(&p).unwrap();
        f.set_modified(base + std::time::Duration::from_secs(i * 60))
            .unwrap();
    }
    // 对照插件：同样参与总上限竞争（跨插件共用一个总上限）
    let p_other = dir.join("pluginB_000000000000_pid2.txt");
    std::fs::write(&p_other, "dump").unwrap();
    let f = std::fs::OpenOptions::new()
        .write(true)
        .open(&p_other)
        .unwrap();
    f.set_modified(base - std::time::Duration::from_secs(3600))
        .unwrap();

    super::PluginInvokerImpl::sweep_freeze_dump_dir(&dir, 10);

    let mut remaining: Vec<String> = std::fs::read_dir(&dir)
        .unwrap()
        .filter_map(|e| e.ok())
        .map(|e| e.file_name().to_string_lossy().to_string())
        .collect();
    remaining.sort();
    assert_eq!(remaining.len(), 10, "清扫后必须恰好剩 10 份: {remaining:?}");
    // 最旧的 3 份（pluginB + pluginA 前两份）被删，最新 10 份保留
    assert!(
        !remaining.contains(&"pluginB_000000000000_pid2.txt".to_string()),
        "最旧文件必须被清扫: {remaining:?}"
    );
    assert!(
        remaining.contains(&"pluginA_000001000011_pid1.txt".to_string()),
        "最新一份必须保留: {remaining:?}"
    );
    // 幂等：再扫一次无变化（性质断言）
    super::PluginInvokerImpl::sweep_freeze_dump_dir(&dir, 10);
    assert_eq!(std::fs::read_dir(&dir).unwrap().count(), 10, "重复清扫幂等");
}
