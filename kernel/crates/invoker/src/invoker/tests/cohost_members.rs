// @feature: FP-0.2.一 插件协议 | @ci: rust-test
//! 合宿成员管理（member sync/idle_gc/pull_path/respawn/spawn_lock）。

use super::*;
#[tokio::test]
async fn test_sync_member_set_refreshes_union_baseline_with_real_host() {
    // 端到端（真 host.py）：宿主以 {a} 名册 spawn → 分配表扩到 {a,b} →
    // sync_host_member_set 按权威补齐 b（load_member 应答）→ 并集基线必须
    // 同窗刷新为 union({a,b})，后续复用不得整组驱逐。python/SDK 不可用时跳过。
    let ok = std::process::Command::new(python_exe())
        .arg("-c")
        .arg("import agentos_plugin_sdk, mcp")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    if !ok {
        eprintln!("python 或 agentos_plugin_sdk 不可用，跳过 sync 基线端到端");
        return;
    }

    let tmp = tempfile::tempdir().unwrap();
    for pid in ["a_service", "b_service"] {
        let member_dir = tmp.path().join("tools").join(pid);
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
    // 名册只含 a_service：复现「宿主定型后新成员惰性装箱」真机时序
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
    loader.add_manifest(make_light_manifest("b_service", "python server.py"));
    let invoker = Arc::new(PluginInvokerImpl::new(loader));
    invoker.assign_light_host_with("a_service", "light", 6);

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
        .insert(host_key.clone(), vec!["a_service".to_string()]);
    invoker.snapshot_member_fingerprints(&host_key);
    // 补齐前并集基线（{a}），检测时刻拨回 2s 前过 TTL 门
    let union_a = {
        let fps: std::collections::BTreeMap<String, u64> =
            [("a_service".to_string(), 0u64)].into_iter().collect();
        union_fingerprint_of(&host_key, &fps)
    };
    invoker.fingerprints.write().insert(
        host_key.clone(),
        (union_a, Instant::now() - Duration::from_secs(2)),
    );

    // b 惰性装箱（调用入口 resolve_host_key 的真时序）+ 按权威补齐
    invoker.assign_light_host_with("b_service", "light", 6);
    let synced = invoker.sync_host_member_set(&host_key, "b_service").await;
    assert!(synced, "load_member 应答成功：成员集补齐必须成功");

    assert_eq!(
        invoker
            .fingerprints
            .read()
            .get(&host_key)
            .map(|(fp, _)| *fp),
        Some(invoker.host_union_fingerprint(&host_key)),
        "补齐成功后并集基线必须同窗刷新为 union({{a,b}})——否则下一调用整组误杀"
    );
    assert!(
        invoker
            .spawned_members
            .read()
            .get(&host_key)
            .is_some_and(|m| m.iter().any(|x| x == "b_service")),
        "镜像必须并入 b_service"
    );

    // 补齐后的复用：命中缓存复用原客户端，宿主进程不动
    let manifest = invoker
        .loader
        .get_manifest("a_service")
        .expect("前置：a_service manifest 在册");
    let got = invoker
        .reuse_cached_host_fast_path(&host_key, &manifest)
        .await;
    let reused = got.expect("补齐后的复用必须命中缓存");
    assert!(
        Arc::ptr_eq(&reused, &shared),
        "必须复用原客户端实例（不得驱逐重建）"
    );
    assert!(shared.read().await.is_alive().await, "宿主进程必须存活");
    invoker.shutdown_all().await;
}
#[tokio::test]
async fn test_idle_gc_member_level_unload_with_real_host() {
    // 大幅回拨环境前置（见 uptime_supports_backdate）：开机不足 40_000s 跳过
    if !uptime_supports_backdate(40_000) {
        return;
    }
    // GC 成员路径端到端（真宿主）：双成员宿主中 a_service 空闲超阈值、
    // b_service 活跃 → GC 只卸 a_service（进程内），宿主进程与其余成员
    // 存活，服务面收缩且 b_service 照常可调——整组连坐消灭（ADR 2026-09-24）。
    let ok = std::process::Command::new(python_exe())
        .arg("-c")
        .arg("import agentos_plugin_sdk, mcp")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    if !ok {
        eprintln!("python 或 agentos_plugin_sdk 不可用，跳过 GC 成员端到端");
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
    // a_service 空闲远超任何合理阈值（默认 300s；40_000s 对 env 覆盖也免疫）
    backdate_idle(&invoker, "a_service", 40_000);

    invoker.run_idle_gc_pass().await;

    assert!(
        invoker.mcp_clients.read().contains_key(&host_key),
        "多成员宿主不得被整组回收（成员级路径）"
    );
    assert!(shared.read().await.is_alive().await, "宿主进程必须存活");
    assert_eq!(
        invoker.spawned_members.read().get(&host_key).cloned(),
        Some(vec!["b_service".to_string()]),
        "只有超阈值成员被卸出成员集"
    );
    assert!(
        invoker
            .light_packing
            .read()
            .assignments
            .contains_key("a_service"),
        "被卸成员分配条目保留（待重加入标记：宿主下次被调 respawn 时一次性回归，宿主重载风暴 2026-09-25）"
    );
    assert!(
        invoker.light_host_members_drifted(&host_key, "a_service"),
        "被卸成员自己被调用：进程无其服务面，必须触发重装箱 respawn（k→1）"
    );
    assert!(
        !invoker.light_host_members_drifted(&host_key, "b_service"),
        "暂离成员的缺席不构成漂移：其余成员照常复用宿主进程（成员粒度隔离）"
    );
    assert!(
        invoker.member_last_used.read().contains_key("b_service"),
        "活跃成员记账保留"
    );
    let tools = shared.read().await.list_tools().await.expect("tools/list");
    let tools_str = serde_json::to_string(&tools).unwrap();
    assert!(!tools_str.contains("a_service.probe"));
    assert!(tools_str.contains("b_service.probe"));
    let called = shared
        .read()
        .await
        .call_tool("b_service.probe", &serde_json::json!({}))
        .await
        .expect("GC 后剩余成员工具必须照常可调");
    assert!(
        serde_json::to_string(&called).unwrap().contains("value"),
        "工具应答须含探针值"
    );
    invoker.shutdown_all().await;
}
#[tokio::test]
async fn idle_soft_unload_keeps_host_serving_remaining_members() {
    // 大幅回拨环境前置（见 uptime_supports_backdate）：开机不足 40_000s 跳过
    if !uptime_supports_backdate(40_000) {
        return;
    }
    // 隔离纪律（装机版 2026-10-09 修复面，真宿主端到端）：成员空闲软卸载后，
    // **其余成员**的调用必须复用存活宿主进程——暂离是「期望成员集未变、进程
    // 服务面暂时收缩」，不得被漂移检测误判成成员集变化而整组 reload（旧实现
    // 下软卸载后数秒内整组重启循环，monitoring 503 / hindsight 重 spawn 连坐）。
    let ok = std::process::Command::new(python_exe())
        .arg("-c")
        .arg("import agentos_plugin_sdk, mcp")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    if !ok {
        eprintln!("python 或 agentos_plugin_sdk 不可用，跳过软卸载隔离端到端");
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
    let manifest_a = make_light_manifest("a_service", "python server.py");
    let manifest_b = make_light_manifest("b_service", "python server.py");
    loader.add_manifest(manifest_a.clone());
    loader.add_manifest(manifest_b.clone());
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
    backdate_idle(&invoker, "a_service", 40_000);

    invoker.run_idle_gc_pass().await;
    assert_eq!(
        invoker.spawned_members.read().get(&host_key).cloned(),
        Some(vec!["b_service".to_string()]),
        "前置：a_service 已被软卸载（进程服务面收缩）"
    );

    // 修复面：其余成员（b_service）的调用复用同一宿主进程，零 respawn
    let reused = invoker
        .get_or_create_mcp_client(&manifest_b)
        .await
        .expect("其余成员调用必须复用存活宿主，不得整组 reload");
    assert!(
        Arc::ptr_eq(&reused, &shared),
        "软卸载后其余成员必须复用同一宿主进程（不得整组 reload）"
    );
    assert!(
        shared.read().await.is_alive().await,
        "宿主进程必须全程存活（其余成员不受影响）"
    );
    assert_eq!(
        invoker.spawned_members.read().get(&host_key).cloned(),
        Some(vec!["b_service".to_string()]),
        "服务面保持收缩态（无 respawn 重建）"
    );
    // 指纹是 spawn 期望态：暂离不改变并集指纹（分配表未变）
    let (fp, _) = *invoker
        .fingerprints
        .read()
        .get(&host_key)
        .expect("指纹条目应存在");
    assert_eq!(
        fp,
        invoker.host_union_fingerprint(&host_key),
        "软卸载不得改变宿主并集指纹（期望成员集未变）"
    );
    let called = reused
        .read()
        .await
        .call_tool("b_service.probe", &serde_json::json!({}))
        .await
        .expect("其余成员工具必须照常可调");
    assert!(
        serde_json::to_string(&called).unwrap().contains("value"),
        "工具应答须含探针值"
    );
    invoker.shutdown_all().await;
}
#[tokio::test]
async fn pending_rejoin_absence_does_not_mask_new_member_drift() {
    // 排除暂离成员不得弱化真实成员集变化检测：软卸载 guard_a（暂离记账）
    // 之后新成员 guard_c 装箱进同一宿主 → 分配表出现「快照没有且非暂离」的
    // 成员 → 漂移必须照常触发整组 respawn（进程成员集在 spawn 时定格，缺
    // 新成员服务面）。与 test_lazy_member_boxed_into_live_host_triggers_respawn
    // 互补：本例证明暂离排除只豁免暂离成员本身。
    let loader = Arc::new(MockLoader::new());
    let manifest_a = make_light_manifest("guard_a", "python server.py");
    let manifest_b = make_light_manifest("guard_b", "python server.py");
    let manifest_c = make_light_manifest("guard_c", "python server.py");
    loader.add_manifest(manifest_a.clone());
    loader.add_manifest(manifest_b.clone());
    loader.add_manifest(manifest_c.clone());
    let invoker = PluginInvokerImpl::new(loader);

    let host_key = invoker.resolve_host_key(&manifest_a);
    invoker.resolve_host_key(&manifest_b);
    let live = spawn_long_lived_stdio_client().await;
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));
    invoker
        .spawned_members
        .write()
        .insert(host_key.clone(), invoker.host_members(&host_key));
    invoker.fingerprints.write().insert(
        host_key.clone(),
        (invoker.host_union_fingerprint(&host_key), Instant::now()),
    );

    // 软卸载 guard_a（宿主侧摘除与本判定无关，finalize 直接记账）
    invoker.finalize_member_unload(&host_key, "guard_a").await;
    assert!(
        !invoker.light_host_members_drifted(&host_key, "guard_b"),
        "前置：暂离成员的缺席不构成漂移"
    );

    // 新成员 guard_c 装箱进同一宿主（真实成员集变化）
    assert_eq!(invoker.resolve_host_key(&manifest_c), host_key);

    let result = invoker.get_or_create_mcp_client(&manifest_b).await;
    let err = match result {
        Err(e) => e,
        Ok(_) => panic!("新成员装箱必须触发整宿主 respawn，暂离排除不得掩盖真实漂移"),
    };
    assert_eq!(
        err.code.as_deref(),
        Some("HOST_DIR_NOT_FOUND"),
        "respawn 路径证据：{err}"
    );
    wait_until_killed(&live_arc, "整宿主 respawn 必须 kill 旧进程").await;
    assert!(invoker.mcp_clients.read().get(&host_key).is_none());
}
#[tokio::test]
async fn soft_unloaded_member_call_loads_it_back_into_live_host() {
    // 大幅回拨环境前置（见 uptime_supports_backdate）：开机不足 40_000s 跳过
    if !uptime_supports_backdate(40_000) {
        return;
    }
    // 按需载回（真宿主端到端，ADR 2026-10-29）：软卸载 a_service 后，a_service
    // **自己**被调用 → 进程无其服务面 → 按权威（分配表）把它一个载回**同一
    // 进程**（agentos/load_member）：旧进程与其余成员不动、不整组 respawn；
    // 载回后调用恢复、待重加入标记撤销、镜像记账并入。
    let ok = std::process::Command::new(python_exe())
        .arg("-c")
        .arg("import agentos_plugin_sdk, mcp")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    if !ok {
        eprintln!("python 或 agentos_plugin_sdk 不可用，跳过按需载回端到端");
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
    let manifest_a = make_light_manifest("a_service", "python server.py");
    let manifest_b = make_light_manifest("b_service", "python server.py");
    loader.add_manifest(manifest_a.clone());
    loader.add_manifest(manifest_b.clone());
    let invoker = PluginInvokerImpl::new(loader);
    invoker.assign_light_host_with("a_service", "light", 6);
    invoker.assign_light_host_with("b_service", "light", 6);

    let mut client = McpClient::new_stdio(
        python_exe().to_string(),
        vec!["-c".to_string(), driver.clone()],
    );
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
    backdate_idle(&invoker, "a_service", 40_000);

    invoker.run_idle_gc_pass().await;
    assert!(
        invoker
            .pending_rejoin_members
            .read()
            .get(&host_key)
            .is_some_and(|p| p.contains("a_service")),
        "前置：a_service 已软卸载并登记待重加入"
    );

    // 被卸成员自己被调用 → 权威有、进程无 → 载回同一进程（不换进程）
    let reused = invoker
        .get_or_create_mcp_client(&manifest_a)
        .await
        .expect("被卸成员被调用必须恢复服务（按权威载回宿主进程）");
    assert!(
        Arc::ptr_eq(&reused, &shared),
        "成员集补齐不得换进程（旧宿主与其余成员不动）"
    );
    assert!(
        !invoker
            .pending_rejoin_members
            .read()
            .get(&host_key)
            .is_some_and(|p| p.contains("a_service")),
        "载回成功必须撤销待重加入标记"
    );
    assert!(
        invoker
            .spawned_members
            .read()
            .get(&host_key)
            .is_some_and(|m| m.iter().any(|x| x == "a_service")),
        "镜像记账必须随 load_member 应答并入被载回成员"
    );
    let called = reused
        .read()
        .await
        .call_tool("a_service.probe", &serde_json::json!({}))
        .await
        .expect("被卸成员载回后工具体必须照常可调");
    assert!(
        serde_json::to_string(&called).unwrap().contains("value"),
        "工具应答须含探针值"
    );
    invoker.shutdown_all().await;
}
#[tokio::test]
async fn lazy_member_boxed_into_live_host_is_loaded_in_place() {
    // 惰性装箱（真宿主端到端，ADR 2026-10-29）：新成员装箱进已存活未满宿主后，
    // 首次调用按权威（分配表）把它载入**同一进程**（agentos/load_member），
    // 不再整组 respawn——宿主进程、其余成员与它们的在飞调用都不动。
    let ok = std::process::Command::new(python_exe())
        .arg("-c")
        .arg("import agentos_plugin_sdk, mcp")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    if !ok {
        eprintln!("python 或 agentos_plugin_sdk 不可用，跳过惰性装箱载入端到端");
        return;
    }

    let tmp = tempfile::tempdir().unwrap();
    for pid in ["a_service", "c_service"] {
        let member_dir = tmp.path().join("tools").join(pid);
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
sys.exit(host.main(["--group", "light", "--slot", "1", "--members", "a_service"],
                   shared_root=__import__("pathlib").Path(r"{shared_root}")))
"#,
        host_py_dir = host_py.parent().unwrap().display(),
        shared_root = shared_root,
    );

    let loader = Arc::new(MockLoader::new());
    let manifest_a = make_light_manifest("a_service", "python server.py");
    let manifest_c = make_light_manifest("c_service", "python server.py");
    loader.add_manifest(manifest_a.clone());
    loader.add_manifest(manifest_c.clone());
    let invoker = PluginInvokerImpl::new(loader);
    invoker.assign_light_host_with("a_service", "light", 6);

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

    // 新成员装箱进同一未满宿主（指纹仍在 TTL 新鲜窗口内 = 生产时序）
    assert_eq!(
        invoker.resolve_host_key(&manifest_c),
        host_key,
        "未满宿主优先塞入（生产场景：装箱复用已存活宿主）"
    );

    let reused = invoker
        .get_or_create_mcp_client(&manifest_c)
        .await
        .expect("新装箱成员调用必须成功（载入存活宿主，不整组重建）");
    assert!(
        Arc::ptr_eq(&reused, &shared),
        "新成员装箱不得换进程（宿主与其余成员不动）"
    );
    assert!(
        invoker
            .spawned_members
            .read()
            .get(&host_key)
            .is_some_and(|m| m.iter().any(|x| x == "c_service")),
        "镜像记账必须随 load_member 应答并入新成员"
    );
    let called = reused
        .read()
        .await
        .call_tool("c_service.probe", &serde_json::json!({}))
        .await
        .expect("载入后新成员工具体必须可调（旧进程缺其服务面会报 [-32602]）");
    assert!(
        serde_json::to_string(&called).unwrap().contains("value"),
        "工具应答须含探针值"
    );
    invoker.shutdown_all().await;
}
#[tokio::test]
async fn test_idle_gc_skips_member_unload_while_host_serves_inflight_calls() {
    // 大幅回拨环境前置（见 uptime_supports_backdate）：开机不足 40_000s 跳过
    if !uptime_supports_backdate(40_000) {
        return;
    }
    // 宿主在飞门（用户裁定 2026-09-28）：合宿宿主有调用在飞（含停泊长等待的
    // 交互调用）期间，任何成员不得被空闲软卸载——被卸成员下次被调会触发整组
    // respawn（BUG-81 语义：旧进程被放弃保留伺候在飞调用、新进程状态为空），
    // 同宿主上停泊的交互等待被拆成「等待方旧进程 / 响应方新进程」，用户响应
    // 永远送达不了等待（装机版 2026-09-28 12:01-12:06 实测链路：human_interaction
    // 等待 67s 被响应，响应路由到重 spawn 后的新进程报「请求不存在」，等待死于
    // 300s 桥面界）。宿主排空后的下一轮 GC 必须照常软卸载（门是延后不是禁止）。
    // python/SDK 不可用时跳过（与端到端用例同款探针）。
    let ok = std::process::Command::new(python_exe())
        .arg("-c")
        .arg("import agentos_plugin_sdk, mcp")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    if !ok {
        eprintln!("python 或 agentos_plugin_sdk 不可用，跳过 GC 宿主在飞门端到端");
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
    // a_service 空闲远超任何合理阈值（默认 300s；40_000s 对 env 覆盖也免疫）；
    // b_service 活跃且其「停泊调用」以宿主面在飞 guard 表示（enter_sidecar_inflight
    // 双面计数，宿主面足以代表）。
    backdate_idle(&invoker, "a_service", 40_000);
    let parked = invoker.enter_inflight(&host_key);
    assert_eq!(
        invoker.host_inflight(&host_key),
        1,
        "前置：宿主在飞计数在悬"
    );

    invoker.run_idle_gc_pass().await;

    assert!(
        invoker.member_last_used.read().contains_key("a_service"),
        "宿主在飞期间空闲成员不得软卸载（卸载即拆散停泊等待的响应路由）"
    );
    assert_eq!(
        invoker.spawned_members.read().get(&host_key).cloned(),
        Some(vec!["a_service".to_string(), "b_service".to_string()]),
        "成员集不得收缩（服务面保持双成员）"
    );
    assert!(
        !invoker.light_host_members_drifted(&host_key, "a_service"),
        "分配表 = 快照：不得制造待重加入漂移"
    );
    let tools = shared.read().await.list_tools().await.expect("tools/list");
    let tools_str = serde_json::to_string(&tools).unwrap();
    assert!(
        tools_str.contains("a_service.probe"),
        "被保护成员服务面必须完整: {tools_str}"
    );

    // 宿主排空：下一轮 GC 照常软卸载（门是延后不是禁止）
    drop(parked);
    assert_eq!(
        invoker.host_inflight(&host_key),
        0,
        "前置：guard 释放计数归零"
    );
    invoker.run_idle_gc_pass().await;

    assert!(
        !invoker.member_last_used.read().contains_key("a_service"),
        "宿主排空后空闲成员应被正常软卸载"
    );
    assert_eq!(
        invoker.spawned_members.read().get(&host_key).cloned(),
        Some(vec!["b_service".to_string()]),
        "排空后成员集按既有语义收缩"
    );
    invoker.shutdown_all().await;
}
#[tokio::test]
async fn test_reload_member_failure_branches_keep_fingerprint_stale() {
    // 失败两路（真进程应答）：宿主未注册 reload 请求 → 协议错误；宿主应答
    // 缺 reloaded=true → 判失败。两者都必须 Err（watcher 回退整组驱逐），且
    // **不刷新指纹记账**——失败即让 pull 路径按指纹差整组 respawn，语义安全。
    // python/SDK 不可用时跳过（与端到端用例同款探针）。
    let ok = std::process::Command::new(python_exe())
        .arg("-c")
        .arg("import agentos_plugin_sdk, mcp")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    if !ok {
        eprintln!("python 或 agentos_plugin_sdk 不可用，跳过 reload 失败分支");
        return;
    }

    // ① 裸 McpServer（无 reload 请求注册）→ 方法不存在 = 协议错误。
    let plain_driver = r#"
import asyncio
from agentos_plugin_sdk.server import McpServer
server = McpServer(tools={}, resources={}, lifecycle_handlers={})
asyncio.run(server.run())
"#;
    // ② 注册 reload 请求但应答缺 reloaded=true → 判失败。
    let wrong_payload_driver = r#"
import asyncio
from mcp import types
from agentos_plugin_sdk.server import McpServer

async def bad_reload(ctx, params):
    return {"reloaded": False}

server = McpServer(
    tools={}, resources={}, lifecycle_handlers={},
    request_handlers={"agentos/reload_member": (types.RequestParams, bad_reload)},
)
asyncio.run(server.run())
"#;
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);

    for (idx, driver) in [plain_driver, wrong_payload_driver].iter().enumerate() {
        let plugin_id = format!("fail_case_{idx}");
        let host_key = invoker.assign_light_host_with(&plugin_id, "light", 6);
        let mut client = McpClient::new_stdio(
            python_exe().to_string(),
            vec!["-c".to_string(), driver.to_string()],
        );
        client.connect().await.expect("假宿主 spawn 应成功");
        client
            .initialize(&serde_json::json!({}))
            .await
            .expect("握手应成功");
        invoker
            .mcp_clients
            .write()
            .insert(host_key.clone(), Arc::new(tokio::sync::RwLock::new(client)));
        // 预置陈旧指纹：失败路径不得刷新（保持 stale → 回退路径语义正确）。
        invoker
            .fingerprints
            .write()
            .insert(host_key.clone(), (42u64, Instant::now()));

        let err = invoker
            .reload_member(&plugin_id)
            .await
            .expect_err("失败分支必须 Err（触发 watcher 回退 force_unload）");
        assert!(!err.message.is_empty());
        let (fp, _) = *invoker
            .fingerprints
            .read()
            .get(&host_key)
            .expect("指纹条目应存在");
        assert_eq!(fp, 42u64, "失败路径不得刷新指纹（{plugin_id}）");
    }
    invoker.shutdown_all().await;
}
#[tokio::test]
async fn test_reload_member_response_implies_member_on_load_completed() {
    // BUG-19 契约钉：reload_member 应答成功 ⇒ 成员 execute 面已见 on_load 副作用。
    // 机制：宿主换入（swap_member）只重放 initialize 注入，on_load 重放由
    // reload 请求窗口内的定向派发承担（SDK CohostServer._dispatch_member_load）。
    // 缺失时成员静默半死——换入后 caller 接线（on_load 副作用）永远不落地，
    // 后续每次 execute 都空等（实况：tool_schema 换入后工具面恒空，当日 6 次
    // 换入全部失接线直至整组重建）。本用例以真 host.py 端到端钉住：
    // 换入后调用成员工具，必须读到 on_load 写下的状态。
    let ok = std::process::Command::new(python_exe())
        .arg("-c")
        .arg("import agentos_plugin_sdk, mcp")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    if !ok {
        eprintln!("python 或 agentos_plugin_sdk 不可用，跳过 reload on_load 契约端到端");
        return;
    }

    // 成员模块：模块级计数器 + on_load 自增 + probe 工具读取。reload 后模块
    // 被 host 侧 loader 重新 exec（计数器归零），on_load 定向派发把它拨到 1
    // ——probe 返回 1 当且仅当「换入后 on_load 已重跑」。
    let tmp = tempfile::tempdir().unwrap();
    let member_dir = tmp.path().join("tools").join("a_service");
    std::fs::create_dir_all(&member_dir).unwrap();
    std::fs::write(member_dir.join("plugin.json"), r#"{"id": "a_service"}"#).unwrap();
    std::fs::write(
        member_dir.join("server.py"),
        concat!(
            "from agentos_plugin_sdk import AgentOSPlugin\n",
            "plugin = AgentOSPlugin(\"a_service\")\n",
            "_state = {\"on_load_runs\": 0}\n",
            "@plugin.on_load\n",
            "async def _on_load(params):\n",
            "    _state[\"on_load_runs\"] += 1\n",
            "@plugin.tool(name='probe', schema={'type': 'object'})\n",
            "async def probe() -> dict:\n",
            "    return {'on_load_runs': _state[\"on_load_runs\"]}\n",
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
    invoker.assign_light_host_with("a_service", "light", 6);

    let mut client = McpClient::new_stdio(python_exe().to_string(), vec!["-c".to_string(), driver]);
    client.connect().await.expect("真宿主 spawn 应成功");
    client
        .initialize(&serde_json::json!({}))
        .await
        .expect("握手应成功");
    let host_key = "group:light:1".to_string();
    let client_arc = Arc::new(tokio::sync::RwLock::new(client));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&client_arc));
    invoker
        .fingerprints
        .write()
        .insert(host_key.clone(), (42u64, Instant::now()));

    invoker
        .reload_member("a_service")
        .await
        .expect("reload_member 应成功（换入 + on_load 定向派发 + 应答）");

    // 换入后的下一次 execute：必须已见 on_load 副作用（换入即接线完成）。
    let result = client_arc
        .read()
        .await
        .call_tool("a_service.probe", &serde_json::json!({}))
        .await
        .expect("换入后成员工具应可达");
    let text = result["content"][0]["text"]
        .as_str()
        .expect("工具结果应含 text content");
    let payload: serde_json::Value =
        serde_json::from_str(text).expect("工具结果 text 应为 JSON 对象");
    assert_eq!(
        payload["on_load_runs"], 1,
        "reload_member 应答成功 ⇒ 换入实例的 on_load 必须已重跑（execute 前接线完成，BUG-19）"
    );
    invoker.shutdown_all().await;
}
#[tokio::test]
async fn test_reload_member_budget_covers_slow_member_initialization() {
    // BUG-19 预算语义：reload 请求窗口自 SDK 起覆盖「模块重 exec + on_load
    // 重放」（宿主在请求处理内定向派发 on_load 后才应答）——慢而未死的成员
    // 初始化不得被掐断：超时即回退 force_unload 整组驱逐，恰是搅动环境的
    // 放大器（一个成员慢初始化 → 全组陪葬 9s 冷启动）。预算须明显大于
    // 合理的慢初始化上界；真挂死宿主仍由本超时 fail-closed 兜底（Err →
    // watcher 回退整组驱逐）。
    let ok = std::process::Command::new(python_exe())
        .arg("-c")
        .arg("import agentos_plugin_sdk, mcp")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    if !ok {
        eprintln!("python 或 agentos_plugin_sdk 不可用，跳过 reload 预算用例");
        return;
    }
    // 假宿主：reload 应答前睡 16s（模拟慢盘/慢解释器下的重 exec + on_load
    // 重放）。16s > 旧预算 15s（红：被掐断回退驱逐）、< 新预算 30s（绿：等足）。
    let slow_driver = r#"
import asyncio
from mcp import types
from agentos_plugin_sdk.server import McpServer

async def slow_reload(ctx, params):
    await asyncio.sleep(16)
    return {"reloaded": True}

server = McpServer(
    tools={}, resources={}, lifecycle_handlers={},
    request_handlers={"agentos/reload_member": (types.RequestParams, slow_reload)},
)
asyncio.run(server.run())
"#;
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);
    let plugin_id = "slow_member";
    let host_key = invoker.assign_light_host_with(plugin_id, "light", 6);
    let mut client = McpClient::new_stdio(
        python_exe().to_string(),
        vec!["-c".to_string(), slow_driver.to_string()],
    );
    client.connect().await.expect("假宿主 spawn 应成功");
    client
        .initialize(&serde_json::json!({}))
        .await
        .expect("握手应成功");
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::new(tokio::sync::RwLock::new(client)));
    invoker
        .fingerprints
        .write()
        .insert(host_key.clone(), (42u64, Instant::now()));

    invoker
        .reload_member(plugin_id)
        .await
        .expect("慢而未死的成员初始化必须等足预算完成，而非超时回退整组驱逐（BUG-19）");
    let (fp, _) = *invoker
        .fingerprints
        .read()
        .get(&host_key)
        .expect("指纹条目应存在");
    assert_eq!(
        fp,
        invoker.host_union_fingerprint(&host_key),
        "慢 reload 完成后指纹记账必须刷新（与快路径同语义）"
    );
    invoker.shutdown_all().await;
}
#[tokio::test]
async fn test_idle_gc_reclaims_whole_host_and_frees_slots() {
    // idle GC 进程级兜底（成员粒度记账后）：组内**最后成员**空闲超阈值 → 走
    // 既有整组回收——kill 宿主进程 + 清缓存/指纹/成员记账 + 分配表条目全部
    // 释放（槽位复用）。多成员宿主走进程内成员卸载（见真宿主 e2e），不整组 kill。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_light_manifest("guard_a", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);

    // 装箱 a → group:light:1；模拟已 spawn 宿主（长驻假进程入缓存）
    invoker.resolve_host_key(&make_light_manifest("guard_a", "python server.py"));
    let host_key = "group:light:1".to_string();
    let live = spawn_long_lived_stdio_client().await;
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));
    invoker.touch_member_last_used("guard_a");
    invoker
        .fingerprints
        .write()
        .insert(host_key.clone(), (42u64, Instant::now()));

    // 回写旧时间戳：空闲 400s > 默认阈值 300s
    invoker.member_last_used.write().insert(
        "guard_a".to_string(),
        Instant::now() - Duration::from_secs(400),
    );

    invoker.run_idle_gc_pass().await;

    wait_until_killed(&live_arc, "最后成员空闲超时必须 kill 宿主进程").await;
    assert!(
        invoker.mcp_clients.read().get(&host_key).is_none(),
        "回收即清空：mcp_clients 条目必须移除"
    );
    assert!(
        invoker.member_last_used.read().get("guard_a").is_none(),
        "回收即清空：成员 last_used 条目必须移除"
    );
    assert!(
        invoker.fingerprints.read().get(&host_key).is_none(),
        "回收即清空：指纹条目必须移除"
    );
    assert!(
        invoker.light_packing.read().assignments.is_empty(),
        "回收即清空：分配表内该宿主全部成员条目必须释放"
    );
}
#[tokio::test]
async fn test_force_unload_after_idle_reclaim_spares_recycled_slot_new_host() {
    // B15-R1 回归锚（watcher 代码指纹 evict × idle 回收交互）：宿主被 idle GC
    // 回收（分配表清空、槽位复用）后，**被回收老成员**的代码指纹 evict
    // （force_unload）不得误杀复用该槽位的新宿主——驱逐按分配表定位宿主，
    // 无分配条目 = 无宿主可杀；正例对照：evict 现任成员杀其宿主且不回收装箱。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_light_manifest("old_m", "python server.py"));
    loader.add_manifest(make_light_manifest("new_n", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);

    // ① 老成员 old_m 装箱 group:light:1，宿主已 spawn（假进程入缓存）+ 记账
    invoker.resolve_host_key(&make_light_manifest("old_m", "python server.py"));
    let host_key = "group:light:1".to_string();
    let host_m = Arc::new(tokio::sync::RwLock::new(
        spawn_long_lived_stdio_client().await,
    ));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&host_m));
    invoker.touch_member_last_used("old_m");

    // ② idle GC 回收：进程 kill + 分配表清空（槽位可复用）
    invoker.member_last_used.write().insert(
        "old_m".to_string(),
        Instant::now() - Duration::from_secs(400),
    );
    invoker.run_idle_gc_pass().await;
    wait_until_killed(&host_m, "前置：空闲宿主已被 GC 回收").await;
    assert!(invoker.light_packing.read().assignments.is_empty());

    // ③ 新成员 new_n 装箱进复用的 group:light:1 并 spawn 新宿主
    let assigned = invoker.resolve_host_key(&make_light_manifest("new_n", "python server.py"));
    assert_eq!(assigned, host_key, "前置：新成员复用被回收槽位");
    let host_n = Arc::new(tokio::sync::RwLock::new(
        spawn_long_lived_stdio_client().await,
    ));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&host_n));
    invoker.touch_member_last_used("new_n");

    // ④ evict 被回收老成员 old_m：无分配条目 → 无宿主可杀 → 新宿主安然无恙
    invoker.force_unload_impl("old_m").await.unwrap();
    assert!(
        host_n.read().await.is_alive().await,
        "evict 被回收成员不得误杀复用槽位的新宿主"
    );
    assert!(
        invoker.mcp_clients.read().contains_key(&host_key),
        "新宿主缓存条目保持存活"
    );

    // ⑤ 正例对照：evict 现任成员 new_n → 其宿主被杀，装箱保留（respawn 按表重建）
    invoker.force_unload_impl("new_n").await.unwrap();
    wait_until_killed(&host_n, "evict 现任成员必须 kill 其宿主进程").await;
    assert!(!invoker.mcp_clients.read().contains_key(&host_key));
    assert!(
        invoker
            .light_packing
            .read()
            .assignments
            .contains_key("new_n"),
        "evict（force_unload）不回收装箱分配——respawn 按表重建成员集"
    );
}
#[tokio::test]
async fn test_idle_gc_skips_never_unload_member() {
    // 成员级永不卸载（ADR 2026-09-24）：声明 idle_timeout_secs=0 的成员自己
    // 不被空闲回收；同宿主他成员超阈值走进程内成员卸载路径（假 stdio 对端
    // 不应答 unload_member → Err 仅 warn 留待下轮，**绝不整组兜底 kill**——
    // 整组兜底会违背 never-unload 成员的保活声明）。
    let loader = Arc::new(MockLoader::new());
    let mut never = make_light_manifest("never_idle", "python server.py");
    never.lifecycle = Some(agentos_core::traits::PluginLifecycle {
        idle_timeout_secs: Some(0),
    });
    loader.add_manifest(never);
    loader.add_manifest(make_light_manifest("guard_b", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);

    invoker.resolve_host_key(&make_light_manifest("never_idle", "python server.py"));
    invoker.resolve_host_key(&make_light_manifest("guard_b", "python server.py"));
    let host_key = "group:light:1".to_string();
    let live = spawn_long_lived_stdio_client().await;
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));
    invoker.member_last_used.write().insert(
        "never_idle".to_string(),
        Instant::now() - Duration::from_secs(400),
    );
    invoker.member_last_used.write().insert(
        "guard_b".to_string(),
        Instant::now() - Duration::from_secs(400),
    );

    invoker.run_idle_gc_pass().await;

    assert!(
        live_arc.read().await.is_alive().await,
        "含永不卸载成员的宿主不得被整组兜底 kill"
    );
    assert!(invoker.mcp_clients.read().get(&host_key).is_some());
    assert!(
        invoker.member_last_used.read().contains_key("never_idle"),
        "never-unload 成员条目保留（未进回收分支）"
    );
}
#[tokio::test]
async fn test_host_union_fingerprint_sensitivity() {
    // 指纹并集（§4.6）：宿主指纹 = 成员指纹并集 + 成员集本身——
    // 成员代码变更、成员集变化均触发变化；同状态稳定。
    let tmp = tempfile::tempdir().unwrap();
    let dir_a = tmp.path().join("guard_a");
    let dir_b = tmp.path().join("guard_b");
    for d in [&dir_a, &dir_b] {
        std::fs::create_dir_all(d).unwrap();
        std::fs::write(d.join("server.py"), b"print(1)").unwrap();
    }
    let loader = Arc::new(MockLoader::new());
    let manifest_a = make_light_manifest("guard_a", "python server.py");
    let manifest_b = make_light_manifest("guard_b", "python server.py");
    loader.add_manifest(manifest_a.clone());
    loader.add_manifest(manifest_b.clone());
    loader
        .plugin_dirs
        .write()
        .insert("guard_a".to_string(), dir_a.to_string_lossy().into_owned());
    loader
        .plugin_dirs
        .write()
        .insert("guard_b".to_string(), dir_b.to_string_lossy().into_owned());
    let invoker = PluginInvokerImpl::new(loader);

    // 成员 {a}
    invoker.resolve_host_key(&manifest_a);
    let fp_a_only = invoker.host_union_fingerprint("group:light:1");
    // 稳定性：同状态两次计算相同
    assert_eq!(fp_a_only, invoker.host_union_fingerprint("group:light:1"));

    // 成员集变化：加入 b → 指纹变化（成员集本身是指纹的一部分）
    invoker.resolve_host_key(&manifest_b);
    let fp_ab = invoker.host_union_fingerprint("group:light:1");
    assert_ne!(fp_a_only, fp_ab, "新成员加入必须改变宿主指纹");

    // 成员代码变更：a 的文件 mtime 变化 → 指纹变化
    std::thread::sleep(std::time::Duration::from_secs_f64(1.1));
    std::fs::write(dir_a.join("server.py"), b"print(2) # changed").unwrap();
    let fp_ab_changed = invoker.host_union_fingerprint("group:light:1");
    assert_ne!(
        fp_ab, fp_ab_changed,
        "任一成员代码变更必须改变宿主指纹（触发整宿主 respawn）"
    );
}
#[tokio::test]
async fn test_member_set_change_triggers_full_host_respawn() {
    // 指纹并集触发整宿主 respawn（§4.6 行为级）：已 spawn 宿主（假进程入缓存）
    // 在新成员加入分配表后，下次调用 fast path 判 stale → kill 整宿主 → 走 respawn
    // 路径（_host 未落地 → HOST_DIR_NOT_FOUND，证明 respawn 真被触发）。
    let loader = Arc::new(MockLoader::new());
    let manifest_a = make_light_manifest("guard_a", "python server.py");
    let manifest_b = make_light_manifest("guard_b", "python server.py");
    loader.add_manifest(manifest_a.clone());
    loader.add_manifest(manifest_b.clone());
    let invoker = PluginInvokerImpl::new(loader);

    let host_key = invoker.resolve_host_key(&manifest_a);
    // 模拟已 spawn：假进程入缓存 + 指纹已记录（回写 2s 前，绕过 TTL 门）
    let live = spawn_long_lived_stdio_client().await;
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));
    invoker.fingerprints.write().insert(
        host_key.clone(),
        (
            invoker.host_union_fingerprint(&host_key),
            Instant::now() - Duration::from_secs(2),
        ),
    );

    // 新成员 b 加入（成员集变化 → 宿主指纹必变）
    invoker.resolve_host_key(&manifest_b);

    let result = invoker.get_or_create_mcp_client(&manifest_a).await;
    // respawn 被触发：host 命令解析先于 spawn 失败（_host 未落地，HOST_DIR_NOT_FOUND）
    let err = match result {
        Err(e) => e,
        Ok(_) => panic!("成员集变化必须触发整宿主 respawn（走 spawn 路径），不得返回缓存实例"),
    };
    assert_eq!(
        err.code.as_deref(),
        Some("HOST_DIR_NOT_FOUND"),
        "respawn 路径证据：{err}"
    );
    // 旧宿主进程被 kill + 缓存逐出
    wait_until_killed(&live_arc, "整宿主 respawn 必须 kill 旧进程").await;
    assert!(invoker.mcp_clients.read().get(&host_key).is_none());
}
#[tokio::test]
async fn test_lazy_member_boxed_into_live_host_triggers_respawn() {
    // 惰性装箱漂移（缺陷回归）：新 light 成员装箱进**已 spawn 的未满宿主**后，
    // 宿主进程成员集必须重建——否则复用旧进程（成员集在 spawn 时定格）会
    // 报 MCP [-32602] tool not found。
    //
    // 与 test_member_set_change_triggers_full_host_respawn 的差异：该测试把
    // 指纹回写 2s 前绕过 TTL 门；本测试指纹保持**新鲜**（spawn 后立即装箱的
    // 真实时序）——缺陷正是 TTL 门（1s）短路了成员集漂移检测，装箱调用本身
    // 落在 TTL 窗口内，fast path 直接复用旧进程。
    let loader = Arc::new(MockLoader::new());
    let manifest_a = make_light_manifest("guard_a", "python server.py");
    let manifest_b = make_light_manifest("guard_b", "python server.py");
    loader.add_manifest(manifest_a.clone());
    loader.add_manifest(manifest_b.clone());
    let invoker = PluginInvokerImpl::new(loader);

    // 成员 a 装箱 + 模拟已 spawn 宿主（假进程入缓存 + 指纹新鲜记录 +
    // spawn 成员集快照——生产路径下 spawn 时写入，见 spawned_members 注释）
    let host_key = invoker.resolve_host_key(&manifest_a);
    assert_eq!(host_key, "group:light:1");
    let live = spawn_long_lived_stdio_client().await;
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));
    invoker
        .spawned_members
        .write()
        .insert(host_key.clone(), invoker.host_members(&host_key));
    invoker.fingerprints.write().insert(
        host_key.clone(),
        (invoker.host_union_fingerprint(&host_key), Instant::now()),
    );

    // 新成员 b 惰性装箱进同一未满宿主（成员集变化，指纹新鲜窗口内）
    assert_eq!(
        invoker.resolve_host_key(&manifest_b),
        host_key,
        "未满宿主优先塞入（缺陷场景：装箱复用已 spawn 宿主）"
    );

    let result = invoker.get_or_create_mcp_client(&manifest_a).await;
    // respawn 必须被触发：host 命令解析先于 spawn 失败（_host 未落地，
    // HOST_DIR_NOT_FOUND）——不得返回缓存实例（旧进程成员集无 b）
    let err = match result {
        Err(e) => e,
        Ok(_) => panic!("装箱到已存活宿主后必须触发整宿主 respawn，不得复用旧进程"),
    };
    assert_eq!(
        err.code.as_deref(),
        Some("HOST_DIR_NOT_FOUND"),
        "respawn 路径证据：{err}"
    );
    // 旧宿主进程被 kill + 缓存逐出
    wait_until_killed(&live_arc, "整宿主 respawn 必须 kill 旧进程").await;
    assert!(invoker.mcp_clients.read().get(&host_key).is_none());
}
#[tokio::test]
async fn test_missing_spawn_snapshot_with_live_host_rebuilds_on_lazy_pack() {
    // BUG-44 回归（竞态遗留态）：快路径驱逐（kill 因在飞调用持读锁阻塞数秒）
    // 与并发 respawn 交错时，驱逐的 spawned_members.remove 段可能落在 respawn
    // 的「build 写快照 → connect → 缓存写入」窗口内，留下「缓存存活但快照缺失」
    // 的宿主（kernel.log 2026-09-18 00:21:20-35 实测序列）。
    //
    // 快照缺失 = 进程成员集未知——新成员装箱进该宿主后的首调用必须触发整宿主
    // respawn（按当前分配表重建），不得复用成员集未知的旧进程：复用即
    // MCP [-32602] tool not found（BUG-44 T1：mode_writing.mode.get_profile
    // 2ms 即败，装箱调用本身落在指纹 TTL 窗口内，指纹检测被门蔽）。
    // 指纹保持新鲜 = 真实时序（T1 距上次指纹刷新仅 0.16s）。
    let loader = Arc::new(MockLoader::new());
    let manifest_a = make_light_manifest("guard_a", "python server.py");
    let manifest_b = make_light_manifest("guard_b", "python server.py");
    loader.add_manifest(manifest_a.clone());
    loader.add_manifest(manifest_b.clone());
    let invoker = PluginInvokerImpl::new(loader);

    let host_key = invoker.resolve_host_key(&manifest_a);
    let live = spawn_long_lived_stdio_client().await;
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));
    // 快照缺失（竞态遗留态）——刻意不写 spawned_members
    invoker.fingerprints.write().insert(
        host_key.clone(),
        (invoker.host_union_fingerprint(&host_key), Instant::now()),
    );

    // 新成员 b 装箱进同一未满宿主（分配表成员集变化，快照无法作证）
    assert_eq!(
        invoker.resolve_host_key(&manifest_b),
        host_key,
        "未满宿主优先塞入（装箱复用已 spawn 宿主）"
    );

    let result = invoker.get_or_create_mcp_client(&manifest_a).await;
    // respawn 必须被触发：host 命令解析先于 spawn 失败（_host 未落地，
    // HOST_DIR_NOT_FOUND）——不得返回缓存实例（旧进程成员集未知）
    let err = match result {
        Err(e) => e,
        Ok(_) => panic!("快照缺失（成员集未知）的存活宿主必须整宿主 respawn，不得复用旧进程"),
    };
    assert_eq!(
        err.code.as_deref(),
        Some("HOST_DIR_NOT_FOUND"),
        "respawn 路径证据：{err}"
    );
    // 旧宿主进程被 kill + 缓存逐出
    wait_until_killed(&live_arc, "成员集未知的宿主必须 kill 重建").await;
    assert!(invoker.mcp_clients.read().get(&host_key).is_none());
}
#[test]
fn diff_member_fingerprints_four_quadrants() {
    // 纯函数四象限：无变 / 集合变（增/删）/ 单成员变（真实指纹构造下的仅代码
    // 变与仅配置变——两类变更同归 Changed，即定向 reload_member 的对象集）。
    let mut base: std::collections::BTreeMap<String, u64> = std::collections::BTreeMap::new();
    base.insert("m1".to_string(), 1);
    base.insert("m2".to_string(), 2);

    // 无变
    assert_eq!(
        diff_member_fingerprints(&base, &base),
        MemberFingerprintDiff::NoChange,
        "逐成员全同 = 无热换对象"
    );

    // 集合变（增/删成员均属集合变化，必须整组重建）
    let mut added = base.clone();
    added.insert("m3".to_string(), 3);
    assert_eq!(
        diff_member_fingerprints(&base, &added),
        MemberFingerprintDiff::SetChanged,
        "新成员装箱 = 集合变"
    );
    let mut removed = base.clone();
    removed.remove("m2");
    assert_eq!(
        diff_member_fingerprints(&base, &removed),
        MemberFingerprintDiff::SetChanged,
        "成员移除 = 集合变"
    );

    // 仅代码变 / 仅配置变（真实指纹构造）：成员集不变、单成员指纹变化
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("m1");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("server.py"), b"print(1)").unwrap();
    let cfg_path = tmp.path().join("cfg.yaml");
    std::fs::write(&cfg_path, b"k: v1").unwrap();
    let loader = Arc::new(MockLoader::new());
    let mut manifest = make_light_manifest("m1", "python server.py");
    manifest.config_files = vec![ConfigFileMapping {
        id: "cfg".to_string(),
        path: cfg_path.to_string_lossy().into_owned(),
        label: String::new(),
        target: None,
        settings: None,
        fields: vec![],
    }];
    loader.add_manifest(manifest.clone());
    loader
        .plugin_dirs
        .write()
        .insert("m1".to_string(), dir.to_string_lossy().into_owned());
    let invoker = PluginInvokerImpl::new(loader);

    let fp0 = invoker.resolve_fingerprint(&manifest);
    // 仅代码变：mtime 变化（指纹秒级+纳秒精度，回写前 sleep 越过粒度）
    std::thread::sleep(std::time::Duration::from_secs_f64(1.1));
    std::fs::write(dir.join("server.py"), b"print(2) # changed").unwrap();
    let fp_code = invoker.resolve_fingerprint(&manifest);
    assert_ne!(fp_code, fp0, "代码文件变更必须改变成员指纹");
    let mut table = std::collections::BTreeMap::new();
    table.insert("m1".to_string(), fp0);
    let mut current = std::collections::BTreeMap::new();
    current.insert("m1".to_string(), fp_code);
    assert_eq!(
        diff_member_fingerprints(&table, &current),
        MemberFingerprintDiff::Changed(vec!["m1".to_string()]),
        "仅代码变 = 单成员 Changed"
    );

    // 仅配置变：代码回写后以代码变后指纹为新基准，改 config_files 指向文件
    std::thread::sleep(std::time::Duration::from_secs_f64(1.1));
    std::fs::write(&cfg_path, b"k: v2").unwrap();
    let fp_cfg = invoker.resolve_fingerprint(&manifest);
    assert_ne!(fp_cfg, fp_code, "config_files 变更必须改变成员指纹");
    let mut table = std::collections::BTreeMap::new();
    table.insert("m1".to_string(), fp_code);
    let mut current = std::collections::BTreeMap::new();
    current.insert("m1".to_string(), fp_cfg);
    assert_eq!(
        diff_member_fingerprints(&table, &current),
        MemberFingerprintDiff::Changed(vec!["m1".to_string()]),
        "仅配置变 = 同样单成员 Changed（定向热换含声明面）"
    );
}
#[test]
fn host_union_fingerprint_matches_legacy_inline_construction() {
    // D2 重构等价钉：并集指纹改由每成员表派生后，哈希构造与原「排序成员逐个
    // pid|fp」遍历版逐字节相同（pull/watcher 两条路径对旧记账值零漂移）。
    let tmp = tempfile::tempdir().unwrap();
    let loader = Arc::new(MockLoader::new());
    let mut manifests = Vec::new();
    for pid in ["guard_a", "guard_b"] {
        let dir = tmp.path().join(pid);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("server.py"), b"print(1)").unwrap();
        let m = make_light_manifest(pid, "python server.py");
        loader.add_manifest(m.clone());
        loader
            .plugin_dirs
            .write()
            .insert(pid.to_string(), dir.to_string_lossy().into_owned());
        manifests.push(m);
    }
    let invoker = PluginInvokerImpl::new(loader);
    invoker.resolve_host_key(&manifests[0]);
    invoker.resolve_host_key(&manifests[1]);
    let host_key = "group:light:1".to_string();

    use std::collections::hash_map::DefaultHasher;
    use std::hash::Hasher;
    let mut hasher = DefaultHasher::new();
    hasher.write(host_key.as_bytes());
    for pid in invoker.host_members(&host_key) {
        let fp = invoker
            .loader
            .get_manifest(&pid)
            .map_or(0, |m| invoker.resolve_fingerprint(&m));
        hasher.write(pid.as_bytes());
        hasher.write(b"|");
        hasher.write(&fp.to_le_bytes());
    }
    let legacy = hasher.finish();
    assert_eq!(
        invoker.host_union_fingerprint(&host_key),
        legacy,
        "每成员表派生的并集必须与原遍历构造逐字节一致"
    );
    assert_eq!(
        invoker.host_union_fingerprint(&host_key),
        union_fingerprint_of(&host_key, &invoker.current_member_fingerprints(&host_key)),
        "并集 = 同一次遍历产出的每成员表的合成值"
    );
}
#[tokio::test]
async fn pull_path_member_swap_avoids_eviction_e2e() {
    // D2 两段式成功沿（真 host.py 端到端）：双成员宿主 spawn 记账后仅 a_service
    // 代码变更 → fast path 判 stale → 成员级定位（每成员表 diff）→ 定向
    // reload_member 进程内换入 → 宿主进程/连接不动（同一客户端 Arc 复用），
    // 且 spawn 每成员指纹表同步刷新（否则 pull diff 永远误报该成员仍需热换）。
    let ok = std::process::Command::new(python_exe())
        .arg("-c")
        .arg("import agentos_plugin_sdk, mcp")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    if !ok {
        eprintln!("python 或 agentos_plugin_sdk 不可用，跳过 pull 成员级热换端到端");
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
                pid = pid
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
    let manifest_a = make_light_manifest("a_service", "python server.py");
    let manifest_b = make_light_manifest("b_service", "python server.py");
    loader.add_manifest(manifest_a.clone());
    loader.add_manifest(manifest_b.clone());
    // 指纹定位面：成员目录登记（spawn 每成员指纹表按目录 stat 构造）
    for (dirname, pid) in [("a_service", "a_service"), ("b_service", "b_service")] {
        loader.plugin_dirs.write().insert(
            pid.to_string(),
            tmp.path()
                .join("tools")
                .join(dirname)
                .to_string_lossy()
                .into_owned(),
        );
    }
    let invoker = PluginInvokerImpl::new(loader);
    assert_eq!(
        invoker.resolve_host_key(&manifest_a),
        "group:light:1",
        "前置：a_service 装箱 group:light:1"
    );
    assert_eq!(invoker.resolve_host_key(&manifest_b), "group:light:1");
    let host_key = "group:light:1".to_string();

    // spawn 记账（与生产 build_sidecar_transport_client 同点同内容）：
    // 成员集快照 + spawn 每成员指纹表（变更前的磁盘态）。
    let mut client = McpClient::new_stdio(python_exe().to_string(), vec!["-c".to_string(), driver]);
    client.connect().await.expect("真宿主 spawn 应成功");
    client
        .initialize(&serde_json::json!({}))
        .await
        .expect("握手应成功");
    let client_arc = Arc::new(tokio::sync::RwLock::new(client));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&client_arc));
    invoker
        .spawned_members
        .write()
        .insert(host_key.clone(), invoker.host_members(&host_key));
    invoker.snapshot_member_fingerprints(&host_key);
    // 预置「变更前」并集指纹（回写 2s 前绕过 TTL 门）——a_service 改码后必判 stale
    let union_before = invoker.host_union_fingerprint(&host_key);
    invoker.fingerprints.write().insert(
        host_key.clone(),
        (union_before, Instant::now() - Duration::from_secs(2)),
    );
    let table_before = invoker
        .host_member_fingerprints
        .read()
        .get(&host_key)
        .cloned();

    // 仅 a_service 代码变更（b_service 毫厘未动）——v2 为合法成员模块
    // （保留 plugin 对象与工具声明，仅行为面变化，宿主可正常换入）
    std::thread::sleep(std::time::Duration::from_secs_f64(1.1));
    let server_v2 = tmp.path().join("tools").join("a_service").join("server.py");
    std::fs::write(
        &server_v2,
        concat!(
            "from agentos_plugin_sdk import AgentOSPlugin\n",
            "plugin = AgentOSPlugin(\"a_service\")\n",
            "@plugin.tool(name='probe', schema={'type': 'object'})\n",
            "async def probe() -> dict:\n",
            "    return {'value': 2}\n",
        ),
    )
    .unwrap();

    // fast path 两段式：成员级热换成功 → 返回同一客户端实例（宿主进程不动）
    let reused = invoker
        .get_or_create_mcp_client(&manifest_a)
        .await
        .expect("成员级热换成功必须复用宿主，不得走 respawn 报错");
    assert!(
        Arc::ptr_eq(&reused, &client_arc),
        "成员级热换成功必须返回同一客户端实例（宿主进程与连接不动）"
    );
    assert!(
        client_arc.read().await.is_alive().await,
        "成员级热换成功不得驱逐宿主进程"
    );
    assert!(invoker.mcp_clients.read().contains_key(&host_key));
    // spawn 每成员指纹表已刷新该成员条目（进程内代码 = 磁盘现码）
    let fp_now = invoker.resolve_fingerprint(&manifest_a);
    let table_now = invoker
        .host_member_fingerprints
        .read()
        .get(&host_key)
        .cloned();
    assert_ne!(
        table_before.as_ref().and_then(|t| t.get("a_service")),
        Some(&fp_now),
        "前置失效：变更前表值不应等于变更后指纹"
    );
    assert_eq!(
        table_now.as_ref().and_then(|t| t.get("a_service")),
        Some(&fp_now),
        "热换成功必须刷新该成员的 spawn 指纹表条目（否则 pull diff 永远误报）"
    );
    // 未变更成员条目保持 spawn 时值（未被无关刷新波及）
    assert_eq!(
        table_now.as_ref().and_then(|t| t.get("b_service")),
        table_before.as_ref().and_then(|t| t.get("b_service")),
        "未变更成员的表条目不得漂移"
    );

    // 后续调用不再误判 stale（并集已随 reload 刷新）——同一实例稳定复用
    let again = invoker
        .get_or_create_mcp_client(&manifest_a)
        .await
        .expect("热换后 pull 不得再误判 stale");
    assert!(
        Arc::ptr_eq(&again, &client_arc),
        "指纹刷新后 pull 复用同一宿主实例"
    );
    invoker.shutdown_all().await;
}
#[tokio::test]
async fn pull_path_member_swap_failure_falls_back_to_whole_rebuild_e2e() {
    // D2 两段式失败沿（真进程应答）：宿主未注册 reload_member 请求（旧宿主
    // 形态）→ 协议错误 → 必须回退既有整组驱逐重建，语义终点与现状一致
    // （旧进程被 kill、缓存逐出、下次调用走 respawn）。
    let ok = std::process::Command::new(python_exe())
        .arg("-c")
        .arg("import agentos_plugin_sdk, mcp")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    if !ok {
        eprintln!("python 或 agentos_plugin_sdk 不可用，跳过 pull 热换失败回退端到端");
        return;
    }
    let plain_driver = r#"
import asyncio
from agentos_plugin_sdk.server import McpServer
server = McpServer(tools={}, resources={}, lifecycle_handlers={})
asyncio.run(server.run())
"#;

    let tmp = tempfile::tempdir().unwrap();
    let member_dir = tmp.path().join("a_service");
    std::fs::create_dir_all(&member_dir).unwrap();
    std::fs::write(member_dir.join("server.py"), b"print(1)").unwrap();
    let loader = Arc::new(MockLoader::new());
    let manifest_a = make_light_manifest("a_service", "python server.py");
    loader.add_manifest(manifest_a.clone());
    loader.plugin_dirs.write().insert(
        "a_service".to_string(),
        member_dir.to_string_lossy().into_owned(),
    );
    let invoker = PluginInvokerImpl::new(loader);
    let host_key = invoker.resolve_host_key(&manifest_a);

    let mut client = McpClient::new_stdio(
        python_exe().to_string(),
        vec!["-c".to_string(), plain_driver.to_string()],
    );
    client.connect().await.expect("假宿主 spawn 应成功");
    client
        .initialize(&serde_json::json!({}))
        .await
        .expect("握手应成功");
    let client_arc = Arc::new(tokio::sync::RwLock::new(client));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&client_arc));
    invoker
        .spawned_members
        .write()
        .insert(host_key.clone(), invoker.host_members(&host_key));
    invoker.snapshot_member_fingerprints(&host_key);
    let union_before = invoker.host_union_fingerprint(&host_key);
    invoker.fingerprints.write().insert(
        host_key.clone(),
        (union_before, Instant::now() - Duration::from_secs(2)),
    );

    // 成员代码变更 → stale → 热换请求被宿主以协议错误拒绝 → 回退整组重建
    std::thread::sleep(std::time::Duration::from_secs_f64(1.1));
    std::fs::write(member_dir.join("server.py"), b"print(2) # changed").unwrap();

    let err = match invoker.get_or_create_mcp_client(&manifest_a).await {
        Err(e) => e,
        Ok(_) => panic!("reload 失败必须回退整组驱逐并走 respawn，不得复用旧进程"),
    };
    assert_eq!(
        err.code.as_deref(),
        Some("HOST_DIR_NOT_FOUND"),
        "回退路径证据：respawn 真被触发（{err}）"
    );
    wait_until_killed(&client_arc, "回退整组驱逐必须 kill 旧宿主进程").await;
    assert!(
        invoker.mcp_clients.read().get(&host_key).is_none(),
        "回退路径必须逐出缓存条目"
    );
    invoker.shutdown_all().await;
}
#[tokio::test]
async fn pull_path_member_set_change_with_table_present_still_rebuilds() {
    // 集合变象限（表在场的判定面）：spawn 每成员指纹表**存在**但键集与当前
    // 分配表不一致（新成员装箱）→ SetChanged → 不试热换直接整组重建——
    // 进程成员集 spawn 时定格，无法进程内补齐（与表缺失的 fail-closed 回退
    // 是不同分支，此处证明判定来自 diff 而非表缺席）。
    let loader = Arc::new(MockLoader::new());
    let manifest_a = make_light_manifest("guard_a", "python server.py");
    let manifest_b = make_light_manifest("guard_b", "python server.py");
    loader.add_manifest(manifest_a.clone());
    loader.add_manifest(manifest_b.clone());
    let invoker = PluginInvokerImpl::new(loader);

    let host_key = invoker.resolve_host_key(&manifest_a);
    let live = spawn_long_lived_stdio_client().await;
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));
    invoker
        .spawned_members
        .write()
        .insert(host_key.clone(), invoker.host_members(&host_key));
    invoker.snapshot_member_fingerprints(&host_key);
    invoker.fingerprints.write().insert(
        host_key.clone(),
        (
            invoker.host_union_fingerprint(&host_key),
            Instant::now() - Duration::from_secs(2),
        ),
    );

    // 新成员装箱进同一未满宿主（成员集变化，表仍在场但键集过时）
    assert_eq!(invoker.resolve_host_key(&manifest_b), host_key);

    let result = invoker.get_or_create_mcp_client(&manifest_a).await;
    let err = match result {
        Err(e) => e,
        Ok(_) => panic!("集合变必须整组重建，不得成员级热换或复用旧进程"),
    };
    assert_eq!(
        err.code.as_deref(),
        Some("HOST_DIR_NOT_FOUND"),
        "respawn 路径证据：{err}"
    );
    wait_until_killed(&live_arc, "集合变必须 kill 旧宿主进程").await;
    assert!(invoker.mcp_clients.read().get(&host_key).is_none());
}
#[tokio::test]
async fn pull_path_no_change_diff_falls_back_to_rebuild_on_driven_stale() {
    // 无变象限：stale 由漂移检测先行触发（调用者软卸载后被调用 = 进程服务面
    // 无它，须 respawn 带回），但逐成员指纹全同 → diff 无热换对象 → 回退整组
    // 重建（respawn 是把暂离成员带回进程的唯一途径，语义与现状一致）。
    let loader = Arc::new(MockLoader::new());
    let manifest_a = make_light_manifest("guard_a", "python server.py");
    let manifest_b = make_light_manifest("guard_b", "python server.py");
    loader.add_manifest(manifest_a.clone());
    loader.add_manifest(manifest_b.clone());
    let invoker = PluginInvokerImpl::new(loader);

    let host_key = invoker.resolve_host_key(&manifest_a);
    assert_eq!(invoker.resolve_host_key(&manifest_b), host_key);
    let live = spawn_long_lived_stdio_client().await;
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));
    invoker
        .spawned_members
        .write()
        .insert(host_key.clone(), invoker.host_members(&host_key));
    invoker.snapshot_member_fingerprints(&host_key);
    invoker.fingerprints.write().insert(
        host_key.clone(),
        (
            invoker.host_union_fingerprint(&host_key),
            Instant::now() - Duration::from_secs(2),
        ),
    );

    // 模拟调用者 a 软卸载暂离（进程服务面收缩、分配条目保留）——a 再被调用
    // 即触发漂移 stale，而磁盘指纹无任何变化
    invoker
        .spawned_members
        .write()
        .get_mut(&host_key)
        .unwrap()
        .retain(|m| m != "guard_a");

    let result = invoker.get_or_create_mcp_client(&manifest_a).await;
    let err = match result {
        Err(e) => e,
        Ok(_) => panic!("无热换对象的 stale 必须回退整组重建（respawn 带回暂离成员）"),
    };
    assert_eq!(
        err.code.as_deref(),
        Some("HOST_DIR_NOT_FOUND"),
        "respawn 路径证据：{err}"
    );
    wait_until_killed(&live_arc, "回退重建必须 kill 旧宿主进程").await;
}
#[tokio::test]
async fn solo_host_stale_still_directly_evicts_without_member_swap() {
    // 独占宿主非回归：无进程内重载面（reload_member 契约 Err），stale 直接走
    // 既有整组驱逐 respawn，不经成员级热换分支。
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("solo_p");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("server.py"), b"print(1)").unwrap();
    let loader = Arc::new(MockLoader::new());
    let manifest = make_sidecar_manifest("solo_p", "python server.py");
    loader.add_manifest(manifest.clone());
    loader
        .plugin_dirs
        .write()
        .insert("solo_p".to_string(), dir.to_string_lossy().into_owned());
    let invoker = PluginInvokerImpl::new(loader);

    let host_key = invoker.resolve_host_key(&manifest);
    assert_eq!(host_key, "plugin:solo_p");
    let live = spawn_long_lived_stdio_client().await;
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));
    invoker.fingerprints.write().insert(
        host_key.clone(),
        (
            invoker.resolve_fingerprint(&manifest),
            Instant::now() - Duration::from_secs(2),
        ),
    );

    // 代码变更 → stale（独占指纹 = 插件自身指纹）
    std::thread::sleep(std::time::Duration::from_secs_f64(1.1));
    std::fs::write(dir.join("server.py"), b"print(2) # changed").unwrap();

    let result = invoker.get_or_create_mcp_client(&manifest).await;
    // respawn 被触发：venv 单轨解析失败（目录无 pyproject.toml）= spawn 路径证据
    let code = match result {
        Ok(_) => panic!("独占 stale 必须直接 respawn，不得复用旧进程"),
        Err(e) => e.code.unwrap_or_default(),
    };
    assert!(
        code == "PYPROJECT_MISSING" || code == "VENV_INTERPRETER_MISSING",
        "独占 stale 必须直接 respawn（错误码 {code}）"
    );
    wait_until_killed(&live_arc, "独占 stale 驱逐必须 kill 旧进程").await;
    assert!(invoker.mcp_clients.read().get(&host_key).is_none());
}
#[tokio::test]
async fn test_blocked_eviction_cannot_delete_concurrently_respawned_snapshot() {
    // BUG-44 根因回归（驱逐/respawn 串行化）：快路径驱逐的 kill 段会因在飞
    // 调用持读锁而阻塞，阻塞期间缓存条目被并发 respawn 替换（新实例 + 新快照）
    // 时，驱逐苏醒后的 remove 段不得误删新条目与新快照——否则留下「缓存存活
    // 但快照缺失」的宿主（漂移检测盲区，见
    // test_missing_spawn_snapshot_with_live_host_rebuilds_on_lazy_pack）。
    let loader = Arc::new(MockLoader::new());
    let manifest_a = make_light_manifest("guard_a", "python server.py");
    loader.add_manifest(manifest_a.clone());
    let invoker = Arc::new(PluginInvokerImpl::new(loader));

    let host_key = invoker.resolve_host_key(&manifest_a);
    let old = spawn_long_lived_stdio_client().await;
    let old_arc = Arc::new(tokio::sync::RwLock::new(old));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&old_arc));
    invoker
        .spawned_members
        .write()
        .insert(host_key.clone(), invoker.host_members(&host_key));
    // 预置过期且错误的指纹（TTL 已过 + 值必不匹配）——确保走 stale 驱逐分支
    invoker.fingerprints.write().insert(
        host_key.clone(),
        (42u64, Instant::now() - Duration::from_secs(2)),
    );

    // 在飞调用持读锁（kill 阻塞前提），并发触发驱逐（指纹已回写 2s 前必判 stale）
    let reader = old_arc.read().await;
    let invoker_ref = Arc::clone(&invoker);
    let manifest_ref = manifest_a.clone();
    let key_ref = host_key.clone();
    let evict_task = tokio::spawn(async move {
        invoker_ref
            .reuse_cached_host_fast_path(&key_ref, &manifest_ref)
            .await
    });
    tokio::time::sleep(Duration::from_millis(100)).await;

    // 阻塞窗口内并发 respawn 完成：缓存条目替换为新实例 + 新成员集快照
    let fresh = spawn_long_lived_stdio_client().await;
    let fresh_arc = Arc::new(tokio::sync::RwLock::new(fresh));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&fresh_arc));
    let mut respawned_members = invoker.host_members(&host_key);
    respawned_members.push("guard_b".to_string());
    respawned_members.sort();
    invoker
        .spawned_members
        .write()
        .insert(host_key.clone(), respawned_members.clone());

    // 放行在飞调用 → 阻塞中的驱逐苏醒
    drop(reader);
    let evicted = evict_task.await.expect("驱逐任务不应 panic");
    assert!(
        evicted.is_none(),
        "stale 判定必须驱逐（返回 None 进 slow path）"
    );

    // 新条目与新快照必须原样保留（驱逐的 remove 段不得落到替换后的状态上）
    let cached_after = invoker
        .mcp_clients
        .read()
        .get(&host_key)
        .cloned()
        .expect("并发 respawn 写入的新缓存条目不得被迟到驱逐误删");
    assert!(
        Arc::ptr_eq(&cached_after, &fresh_arc),
        "缓存条目必须仍是 respawn 后的新实例"
    );
    assert_eq!(
        invoker.spawned_members.read().get(&host_key).cloned(),
        Some(respawned_members),
        "并发 respawn 写入的新快照不得被迟到驱逐误删"
    );
}
#[tokio::test]
async fn test_spawn_lock_per_host_granularity() {
    // spawn 锁粒度（§4.2 第 4 条）：per-host-key——同宿主成员共享锁条目
    // （串行 spawn），跨宿主各自独立锁条目（并行 spawn）。
    // 锁粒度无外部可观察行为面（真并发 spawn 需 host.py 落地），这里以锁表
    // 键集合 + 同宿主单条目为结构证据；spawn 尝试以 HOST_DIR_NOT_FOUND 失败，
    // 锁条目创建于拿锁阶段，失败路径同样能观察粒度。
    let loader = Arc::new(MockLoader::new());
    let manifest_a = make_light_manifest("guard_a", "python server.py");
    let manifest_b = make_light_manifest("guard_b", "python server.py");
    let solo = make_sidecar_manifest("solo_tool", "definitely_missing_command_98765");
    loader.add_manifest(manifest_a.clone());
    loader.add_manifest(manifest_b.clone());
    loader.add_manifest(solo.clone());
    let invoker = PluginInvokerImpl::new(loader);

    // 同宿主两个成员：spawn 失败（HOST_DIR_NOT_FOUND）但锁条目已按宿主键建立
    let _ = invoker.get_or_create_mcp_client(&manifest_a).await;
    let _ = invoker.get_or_create_mcp_client(&manifest_b).await;
    // 独占宿主：另一个锁条目
    let _ = invoker.get_or_create_mcp_client(&solo).await;

    let mut keys: Vec<String> = invoker.spawn_locks.read().keys().cloned().collect();
    keys.sort();
    assert_eq!(
        keys,
        vec!["group:light:1".to_string(), "plugin:solo_tool".to_string()],
        "同宿主（a/b 共享 group:light:1）单锁条目，跨宿主独立条目"
    );
    // 性质断言：锁条目数 < 已尝试 spawn 的插件数（3 个插件 2 个锁 = 同宿主共享）
    assert!(invoker.spawn_locks.read().len() < 3);
}
