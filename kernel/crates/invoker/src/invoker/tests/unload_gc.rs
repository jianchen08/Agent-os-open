// @feature: FP-0.2.一 插件协议 | @ci: rust-test
//! 卸载回收与结果归一化（shutdown/kill/frozen/idle_gc/normalize/extract）。

use super::*;
#[tokio::test]
async fn force_unload_completes_bounded_while_read_lock_held() {
    // 锁结构回归：外部调用方持有 client 读锁期间触发 force_unload
    // （respawn/崩溃恢复路径的锁形态），必须限时完成——unload 段（取写锁 →
    // on_unload → kill）限时 2s，读锁被在飞调用持有时放弃 kill（kill_on_drop
    // 兜底）照常完成驱逐与记账。修复前该结构 = 写锁无超时 + 同任务持读锁
    // 等写锁 → 永久悬挂（请求卡死、GC 单循环瘫痪）。
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    let live = spawn_long_lived_stdio_client().await;
    assert!(live.is_alive().await, "前置：长驻假 sidecar 必须存活");
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert("plugin:victim".to_string(), Arc::clone(&live_arc));

    // 模拟 attempt_sidecar_pipeline 失败分支的持锁形态：读锁存活期间 unload。
    let reader = live_arc.read().await;
    let started = tokio::time::Instant::now();
    let outcome = tokio::time::timeout(
        std::time::Duration::from_secs(8),
        invoker.force_unload_impl("victim"),
    )
    .await;
    drop(reader);

    assert!(
        outcome.is_ok(),
        "持读锁期间 force_unload 必须限时完成（自等写锁 = 永久悬挂）"
    );
    let unload_result = outcome.expect("timeout 已排除");
    assert!(
        unload_result.is_ok(),
        "放弃 kill 后卸载状态机必须照常收敛: {:?}",
        unload_result.err()
    );
    let elapsed = started.elapsed();
    assert!(
        elapsed >= std::time::Duration::from_secs(2),
        "写锁被持有必须等满 2s 限时（超时保护生效），实际 {elapsed:?}"
    );
    assert!(
        elapsed < std::time::Duration::from_secs(8),
        "unload 不得在读锁争用下悬挂，实际 {elapsed:?}"
    );
    assert!(
        invoker.mcp_clients.read().get("plugin:victim").is_none(),
        "unload 后缓存条目必须驱逐（下次调用 respawn 新实例）"
    );
    // 显式回收残留进程（读锁已释放，写锁立即可得）：Windows 上 kill_on_drop
    // 只杀 cmd 包装进程不杀 ping 孙进程，弃置会让替身占满其自然生命周期。
    live_arc
        .write()
        .await
        .kill()
        .await
        .expect("残留进程显式回收");
    wait_until_killed(&live_arc, "显式回收后进程必须终止").await;
}
#[tokio::test]
async fn kill_and_evict_completes_bounded_and_evicts_promptly_while_read_lock_held() {
    // BUG-81 锁结构回归（get_or_create fast path 驱逐口）：在飞长调用（审批
    // wait_for_choice 24h）持客户端读锁期间，staleness 判定触发
    // kill_and_evict_if_current——驱逐必须**有界完成**且缓存条目**即时摘除**
    // （摘除先于 kill 等待）。修复前该结构 = write().kill() 无超时 inline 等待
    // 且条目在 kill 完成后才摘：tokio RwLock 公平队列让写锁排队期间的全部
    // 后续读者（fast path 的 client.read()）队头阻塞——装机版实测 19:21:40
    // drift kill 与 19:21:39 在飞 wait 相撞，approval_service 504 风暴 5400+ 条。
    // 对齐 unload_host 既有模式：条目即摘 + 有界 kill + kill_on_drop 兜底。
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    let live = spawn_long_lived_stdio_client().await;
    assert!(live.is_alive().await, "前置：长驻假 sidecar 必须存活");
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    let host_key = "group:light:9";
    invoker
        .mcp_clients
        .write()
        .insert(host_key.to_string(), Arc::clone(&live_arc));

    // 模拟在飞长调用的持锁形态：读锁横跨整个驱逐（24h 审批等待的缩影）。
    let reader = live_arc.read().await;
    let started = tokio::time::Instant::now();
    let outcome = tokio::time::timeout(
        std::time::Duration::from_secs(8),
        invoker.kill_and_evict_if_current(host_key, &live_arc),
    )
    .await;

    assert!(
        outcome.is_ok(),
        "持读锁期间 kill_and_evict 必须有界完成（inline 等写锁 = 全宿主队头阻塞）"
    );
    let elapsed = started.elapsed();
    assert!(
        elapsed < std::time::Duration::from_secs(1),
        "驱逐不得为 kill 等待内联阻塞（条目必须先摘、新调用走 respawn），实际 {elapsed:?}"
    );
    assert!(
        invoker.mcp_clients.read().get(host_key).is_none(),
        "驱逐后缓存条目必须即时摘除（修复前 kill 完成后才摘 = 排队窗口）"
    );
    assert!(
        invoker.spawned_members.read().get(host_key).is_none(),
        "spawn 成员集快照与缓存条目同生命周期（身份命中时一并清除）"
    );

    // 收尾：释放读锁后显式回收残留进程（Windows 上 kill_on_drop 只杀包装层）。
    drop(reader);
    live_arc
        .write()
        .await
        .kill()
        .await
        .expect("残留进程显式回收");
    wait_until_killed(&live_arc, "显式回收后进程必须终止").await;
}
#[tokio::test]
async fn reuse_fresh_host_locked_completes_bounded_while_read_lock_held() {
    // BUG-81 锁结构回归（get_or_create slow path double-check 口）：spawn 锁内
    // 复检命中漂移宿主时同样走 write().kill()——在飞长调用持读锁期间必须
    // 有界完成返回 None（转 respawn），不得在持 spawn 锁状态下内联等写锁
    // （该宿主的全部 respawn/复用排队 = 与 fast path 同型的宿主级瘫痪）。
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    let live = spawn_long_lived_stdio_client().await;
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    let host_key = "group:light:10";
    invoker
        .mcp_clients
        .write()
        .insert(host_key.to_string(), Arc::clone(&live_arc));
    // 刻意不写 spawned_members 快照：快照缺失按漂移处理（BUG-44 安全网），
    // double-check 必走 kill 分支——正是与在飞读锁相撞的路径。

    let reader = live_arc.read().await;
    let started = tokio::time::Instant::now();
    let outcome = tokio::time::timeout(
        std::time::Duration::from_secs(8),
        invoker.reuse_fresh_host_locked(host_key, "probe_member"),
    )
    .await;
    drop(reader);

    let reused = outcome.expect("持读锁期间 double-check 必须有界完成（inline 等写锁 = 悬挂）");
    assert!(
        reused.is_none(),
        "漂移宿主必须驱逐不复用（返回 None 转 respawn）"
    );
    let elapsed = started.elapsed();
    assert!(
        elapsed < std::time::Duration::from_secs(3),
        "double-check 驱逐不得内联等满在飞调用（有界 kill），实际 {elapsed:?}"
    );
    assert!(
        invoker.mcp_clients.read().get(host_key).is_none(),
        "double-check 驱逐后缓存条目必须摘除"
    );
    // 显式回收残留进程（读锁已释放，写锁立即可得）。
    live_arc
        .write()
        .await
        .kill()
        .await
        .expect("残留进程显式回收");
    wait_until_killed(&live_arc, "显式回收后进程必须终止").await;
}
#[tokio::test]
async fn kill_and_evict_kills_process_when_unlocked_and_preserves_replaced_entry() {
    // 无争用路径 + BUG-44 身份复核语义保持：
    // - 无读锁争用时 try_write 即得 → kill 真实执行，进程死亡（防"只清缓存不杀进程"假实现）；
    // - 条目已被并发 respawn 替换（Arc 身份不符）→ 只杀本次判定的旧实例，
    //   缓存与 spawn 快照（新实例的）原样保留。
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    let stale = spawn_long_lived_stdio_client().await;
    let stale_arc = Arc::new(tokio::sync::RwLock::new(stale));
    let host_key = "group:light:11";
    invoker
        .mcp_clients
        .write()
        .insert(host_key.to_string(), Arc::clone(&stale_arc));

    // 身份不符形态：缓存条目已被"respawn"替换为另一实例（HTTP client 代位）。
    let fresh: SharedMcpClient = Arc::new(tokio::sync::RwLock::new(McpClient::new_http(
        "http://127.0.0.1:9/mcp",
        HashMap::new(),
        None,
    )));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.to_string(), Arc::clone(&fresh));

    invoker
        .kill_and_evict_if_current(host_key, &stale_arc)
        .await;
    let current = invoker.mcp_clients.read().get(host_key).cloned();
    assert!(
        current.is_some_and(|cur| Arc::ptr_eq(&cur, &fresh)),
        "身份不符时不得动缓存条目（BUG-44：respawn 快照以条目为基准）"
    );
    wait_until_killed(&stale_arc, "旧实例进程必须被真实 kill").await;
    // 显式回收代位条目（HTTP 无子进程，清理缓存即可）。
    invoker.mcp_clients.write().remove(host_key);
}
#[tokio::test]
async fn frozen_sidecar_evaluation_then_unlocked_force_unload_kills_process() {
    // 假死评估接缝回归（与 attempt_sidecar_pipeline 失败分支同构的锁序列）：
    // 读锁内完成判定+取证（evaluate_frozen_sidecar 只需 &client），随后释放
    // 读锁再触发 force_unload——限时完成且进程真实被杀、缓存驱逐。
    // 评估只做判定与取证、绝不在调用方读锁存活时执行回收（自等写锁死锁）。
    assert_python_available();

    // 心跳行 #HB 1000（UNIX 毫秒 ≈ 1970）→ heartbeat_age 恒超 30s 阈值，
    // 进程本身存活（sleep 60s）= 「活着但事件循环假死」的确定性替身。
    let script =
        "import sys, time\nsys.stderr.write('#HB 1000\\n'); sys.stderr.flush()\ntime.sleep(60)\n";
    let mut client = McpClient::new_stdio(
        python_exe(),
        vec!["-u".to_string(), "-c".to_string(), script.to_string()],
    );
    client.connect().await.expect("假死替身可连接");
    assert!(client.is_alive().await, "前置：进程存活（非真死）");
    let live_arc = Arc::new(tokio::sync::RwLock::new(client));

    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    invoker
        .mcp_clients
        .write()
        .insert("plugin:frozen".to_string(), Arc::clone(&live_arc));

    // py-spy 注入为必然缺失的二进制：取证降级告警跳过（判定链其余环节照常
    // 走到，且不拖慢测试）。
    let _spy = EnvVarGuard::set("AGENTOS_PY_SPY_BIN", "definitely_not_py_spy_xyz");

    // 等 stderr 读循环记录心跳（connect 返回与记录之间无同步点）。
    let guard = live_arc.read().await;
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(5);
    while guard.heartbeat_age_secs().is_none() {
        assert!(
            tokio::time::Instant::now() < deadline,
            "替身心跳未被记录（#HB 协议契约）"
        );
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }

    let frozen = invoker
        .evaluate_frozen_sidecar(&guard, "frozen", "call failed: synthetic")
        .await;
    assert!(frozen, "心跳停跳超阈值必须判定假死（应触发 force_unload）");
    drop(guard);

    tokio::time::timeout(
        std::time::Duration::from_secs(8),
        invoker.force_unload_impl("frozen"),
    )
    .await
    .expect("释放读锁后 force_unload 必须限时完成")
    .expect("unload 收敛成功");
    wait_until_killed(&live_arc, "假死自愈必须真实 kill 宿主进程").await;
    assert!(
        invoker.mcp_clients.read().get("plugin:frozen").is_none(),
        "unload 后缓存条目必须驱逐（下次调用 respawn 干净实例）"
    );
}
#[tokio::test]
async fn frozen_evaluation_skips_when_heartbeat_absent_or_fresh() {
    // 判定门两分支：从未上报心跳（旧版本 sidecar / HTTP transport）不判假死；
    // 心跳新鲜（事件循环活着）不判假死——只有停跳超阈值才触发自愈。
    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));

    // ① 无心跳：未连接 stdio 客户端，last_heartbeat_ms 恒 0 → None。
    let silent = McpClient::new_stdio("cat", vec![]);
    assert!(
        !invoker
            .evaluate_frozen_sidecar(&silent, "no_hb", "call failed")
            .await,
        "无心跳数据不得判定假死（平滑兼容分支）"
    );

    // ② 心跳新鲜：替身启动即上报当前时刻（age ≈ 0s < 30s 阈值）。
    assert_python_available();
    let script = "import sys,time\nsys.stderr.write('#HB %d\\n' % int(time.time()*1000)); sys.stderr.flush()\ntime.sleep(60)\n";
    let mut client = McpClient::new_stdio(
        python_exe(),
        vec!["-u".to_string(), "-c".to_string(), script.to_string()],
    );
    client.connect().await.expect("替身可连接");
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(5);
    while client.heartbeat_age_secs().is_none() {
        assert!(
            tokio::time::Instant::now() < deadline,
            "替身心跳未被记录（#HB 协议契约）"
        );
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }
    assert!(
        !invoker
            .evaluate_frozen_sidecar(&client, "fresh_hb", "call failed")
            .await,
        "心跳新鲜不得判定假死（协议/工具错误走既有语义）"
    );
}
#[tokio::test]
async fn frozen_evaluation_circuit_breaker_and_pidless_dump_degrade_to_no_unload() {
    // 熔断分支：10 分钟窗口内已假死 respawn 满 3 次 → 停手只取证，返回
    // false（不触发 force_unload，防循环重启掩盖根因）。同程覆盖取证降级：
    // 客户端已无子进程（pid None）→ 取证跳过告警，判定链不 panic 不阻断。
    assert_python_available();
    let _spy = EnvVarGuard::set("AGENTOS_PY_SPY_BIN", "definitely_not_py_spy_xyz");

    // 假死替身：记录陈旧心跳后 kill（pid None 语义），heartbeat_age 仍超阈值。
    let script =
        "import sys, time\nsys.stderr.write('#HB 1000\\n'); sys.stderr.flush()\ntime.sleep(60)\n";
    let mut client = McpClient::new_stdio(
        python_exe(),
        vec!["-u".to_string(), "-c".to_string(), script.to_string()],
    );
    client.connect().await.expect("替身可连接");
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(5);
    while client.heartbeat_age_secs().is_none() {
        assert!(
            tokio::time::Instant::now() < deadline,
            "替身心跳未被记录（#HB 协议契约）"
        );
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }
    client.kill().await.expect("kill 释放子进程");
    assert!(
        client.pid().await.is_none(),
        "前置：无子进程（取证降级路径）"
    );

    let invoker = PluginInvokerImpl::new(Arc::new(MockLoader::new()));
    invoker.freeze_respawns.write().insert(
        "frozen_cb".to_string(),
        vec![Instant::now(), Instant::now(), Instant::now()],
    );

    assert!(
        !invoker
            .evaluate_frozen_sidecar(&client, "frozen_cb", "call failed")
            .await,
        "熔断窗口超限必须停手（返回 false，不再触发 force_unload）"
    );
}
#[tokio::test]
async fn idle_gc_pass_completes_bounded_while_read_lock_held() {
    // GC 写锁超时回归：宿主空闲超期但在飞调用方持读锁时，GC pass 必须限时
    // 完成而非悬挂——unload 段 2s 超时后放弃 kill（kill_on_drop 兜底），
    // 驱逐与记账照常收敛。修复前无超时写锁 = GC 单循环在长流宿主上等数小时
    // （全局空闲回收瘫痪）。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_light_manifest("gc_a", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);

    invoker.resolve_host_key(&make_light_manifest("gc_a", "python server.py"));
    let host_key = "group:light:1".to_string();
    let live = spawn_long_lived_stdio_client().await;
    assert!(live.is_alive().await, "前置：长驻假 sidecar 必须存活");
    let live_arc = Arc::new(tokio::sync::RwLock::new(live));
    invoker
        .mcp_clients
        .write()
        .insert(host_key.clone(), Arc::clone(&live_arc));
    invoker.touch_member_last_used("gc_a");
    // 回写旧时间戳：空闲 400s > 默认阈值 300s（单成员宿主 = 最后成员，走整组回收）
    invoker.member_last_used.write().insert(
        "gc_a".to_string(),
        Instant::now() - Duration::from_secs(400),
    );

    // 在飞调用方持读锁期间跑 GC pass：限时完成 + 超时保护生效 + 驱逐收敛。
    let reader = live_arc.read().await;
    let started = tokio::time::Instant::now();
    tokio::time::timeout(
        std::time::Duration::from_secs(8),
        invoker.run_idle_gc_pass(),
    )
    .await
    .expect("持读锁期间 GC pass 必须限时完成（无超时写锁 = 永久悬挂）");
    drop(reader);
    let elapsed = started.elapsed();
    assert!(
        elapsed >= Duration::from_secs(2),
        "写锁被持有必须等满 2s 限时（超时保护生效），实际 {elapsed:?}"
    );
    assert!(
        invoker.mcp_clients.read().get(&host_key).is_none(),
        "回收即清空：mcp_clients 条目必须移除"
    );
    assert!(
        invoker.member_last_used.read().get("gc_a").is_none(),
        "回收即清空：成员 last_used 条目必须移除"
    );
    assert!(
        live_arc.read().await.is_alive().await,
        "kill 已因写锁超时放弃：在飞期间进程不被打断（kill_on_drop 在最后一个 Arc 释放时兜底）"
    );
    // 显式回收残留进程（读锁已释放，写锁立即可得）：Windows 上 kill_on_drop
    // 只杀 cmd 包装进程不杀 ping 孙进程，弃置会让替身占满其自然生命周期。
    live_arc
        .write()
        .await
        .kill()
        .await
        .expect("残留进程显式回收");
    wait_until_killed(&live_arc, "显式回收后进程必须终止").await;
}
#[tokio::test]
async fn list_plugin_tools_returns_bounded_error_while_read_lock_held() {
    // G2 回收写锁超时回归：本次新 spawn 的宿主在校验后回收（kill）时，写锁
    // 被并发在飞调用方持有时必须限时返回明确错误，而非无限悬挂。宿主是合法
    // 已初始化实例：缓存条目保留，后续调用照常复用。
    use std::sync::Mutex as StdMutex;

    fn subseq(hay: &[u8], needle: &[u8]) -> Option<usize> {
        hay.windows(needle.len()).position(|w| w == needle)
    }
    fn content_length(headers: &[u8]) -> usize {
        String::from_utf8_lossy(headers)
            .to_ascii_lowercase()
            .lines()
            .find_map(|l| l.strip_prefix("content-length:"))
            .and_then(|v| v.trim().parse().ok())
            .unwrap_or(0)
    }

    // 本地 mock HTTP MCP（环回，出网守卫默认放行）：tools/list 响应延迟
    // 400ms，为并发持锁任务留出确定性抢占窗口；连接内支持 keep-alive 多请求
    // （reqwest 连接池复用：initialize / on_load / tools/list 同连接）。
    async fn spawn_delayed_mock(tools_list_delay: Duration) -> String {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        tokio::spawn(async move {
            loop {
                let Ok((mut sock, _)) = listener.accept().await else {
                    return;
                };
                tokio::spawn(async move {
                    // 帧缓冲跨读保留 + 规范握手（rmcp 客户端并发发
                    // initialize/initialized/tools/list，按 Content-Length 逐帧
                    // 切分；GET 流探测回 405；notification 回 202；id 全回显）。
                    let mut buf: Vec<u8> = Vec::new();
                    let mut tmp = [0u8; 4096];
                    loop {
                        let hdr_end = loop {
                            if let Some(p) = subseq(&buf, b"\r\n\r\n") {
                                break p;
                            }
                            match sock.read(&mut tmp).await {
                                Ok(0) | Err(_) => return,
                                Ok(n) => buf.extend_from_slice(&tmp[..n]),
                            }
                        };
                        let cl = content_length(&buf[..hdr_end]);
                        let frame_end = hdr_end + 4 + cl;
                        while buf.len() < frame_end {
                            match sock.read(&mut tmp).await {
                                Ok(0) | Err(_) => return,
                                Ok(n) => buf.extend_from_slice(&tmp[..n]),
                            }
                        }
                        let (status, payload) = if buf.starts_with(b"GET") {
                            ("HTTP/1.1 405 Method Not Allowed", String::new())
                        } else {
                            let body: serde_json::Value =
                                serde_json::from_slice(&buf[hdr_end + 4..frame_end])
                                    .unwrap_or(Value::Null);
                            let method = body.get("method").and_then(Value::as_str).unwrap_or("");
                            eprintln!("[DELAYED] method={} id={:?}", method, body.get("id"));
                            let is_notification = body.get("id").is_none();
                            let payload = if is_notification {
                                String::new()
                            } else {
                                let id = body.get("id").cloned().unwrap_or(Value::Null);
                                let result = if method == "initialize" {
                                    serde_json::json!({
                                        "protocolVersion": "2025-06-18",
                                        "capabilities": {},
                                        "serverInfo": {"name": "mock", "version": "1.0"}
                                    })
                                } else if method == "tools/list" {
                                    tokio::time::sleep(tools_list_delay).await;
                                    serde_json::json!({"tools": []})
                                } else {
                                    serde_json::json!({"ok": true})
                                };
                                serde_json::json!({"jsonrpc": "2.0", "id": id, "result": result})
                                    .to_string()
                            };
                            if is_notification {
                                ("HTTP/1.1 202 Accepted", payload)
                            } else {
                                ("HTTP/1.1 200 OK", payload)
                            }
                        };
                        let out = format!(
                            "{status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
                            payload.len(),
                            payload
                        );
                        if sock.write_all(out.as_bytes()).await.is_err() {
                            return;
                        }
                        buf.drain(..frame_end);
                    }
                });
            }
        });
        url
    }

    let url = spawn_delayed_mock(Duration::from_millis(400)).await;
    let loader = Arc::new(MockLoader::new());
    let manifest = make_http_manifest("g2_contended", &url);
    loader.add_manifest(manifest.clone());
    let invoker = Arc::new(PluginInvokerImpl::new(loader));

    // 并发持锁任务：缓存条目（HTTP 客户端，get_or_create 插入）一出现即持
    // 读锁并保持到测试尾——模拟并发在飞调用。
    let stop = Arc::new(StdMutex::new(false));
    let holder = {
        let invoker = Arc::clone(&invoker);
        let stop = stop.clone();
        tokio::spawn(async move {
            loop {
                let arc = invoker
                    .mcp_clients
                    .read()
                    .get("plugin:g2_contended")
                    .cloned();
                if let Some(arc) = arc {
                    let _guard = arc.read().await;
                    while !*stop.lock().unwrap() {
                        tokio::time::sleep(Duration::from_millis(20)).await;
                    }
                    return true;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
    };

    let started = tokio::time::Instant::now();
    let result = tokio::time::timeout(
        std::time::Duration::from_secs(10),
        invoker.list_plugin_tools("g2_contended"),
    )
    .await
    .expect("持读锁期间 list_plugin_tools 必须限时返回（无超时写锁 = 永久悬挂）");
    let elapsed = started.elapsed();

    let err = result.expect_err("写锁被持有：回收 kill 必须超时报错而非悬挂");
    assert_eq!(
        err.code.as_deref(),
        Some("HOST_RECLAIM_TIMEOUT"),
        "必须是明确的回收超时错误: {err}"
    );
    assert!(
        elapsed >= Duration::from_secs(2),
        "写锁被持有必须等满 2s 限时（超时保护生效），实际 {elapsed:?}"
    );
    assert!(
        invoker
            .mcp_clients
            .read()
            .get("plugin:g2_contended")
            .is_some(),
        "超时放弃回收：宿主是合法已初始化实例，缓存条目保留供后续调用复用"
    );
    *stop.lock().unwrap() = true;
    assert!(holder.await.expect("持锁任务正常退出"));
}
#[test]
fn test_normalize_native_tool_output_envelope_shapes() {
    // 新工具插件的约定返回：{success, data} / {success:false, error} 信封直用。
    let ok = normalize_native_tool_output(&json!({
        "success": true, "data": {"output": "hi"}, "duration_ms": 7
    }));
    assert!(ok.success);
    assert_eq!(ok.data["output"], "hi");
    assert_eq!(ok.duration_ms, Some(7), "信封带的 duration_ms 应保留");

    // 失败信封可缺 data（serde 直解析会报 missing field，归一层手构造）
    let fail = normalize_native_tool_output(&json!({
        "success": false, "error": "boom"
    }));
    assert!(!fail.success);
    assert_eq!(fail.error.as_deref(), Some("boom"));

    // 失败信封缺 error 字段 → 通用文案，不 panic
    let fail_no_msg = normalize_native_tool_output(&json!({"success": false}));
    assert!(!fail_no_msg.success);
    assert!(fail_no_msg.error.is_some());
}
#[test]
fn test_normalize_native_tool_output_legacy_pipeline_shape_wraps_success() {
    // 旧 pipeline 插件（忽略 tool_call）返回 state_updates → 纯业务数据包
    // success 信封（零破坏）。注意 state_updates 可能天然含 error 键——
    // 无 success 字段一律按业务数据处理，不误判 failure。
    let legacy = normalize_native_tool_output(&json!({
        "processed_by": "test_plugin",
        "error": null
    }));
    assert!(
        legacy.success,
        "无 success 字段 = 旧插件业务数据，包 success"
    );
    assert_eq!(legacy.data["processed_by"], "test_plugin");
    assert!(
        legacy.data.get("error").is_some(),
        "业务数据原样保留在 data"
    );
}
#[test]
fn test_normalize_native_tool_output_success_without_data() {
    // 带 success=true 但无 data → data=Null（不报 missing field）。
    let r = normalize_native_tool_output(&json!({"success": true}));
    assert!(r.success);
    assert_eq!(r.data, serde_json::Value::Null);
}
#[test]
fn test_normalize_mcp_result_python_toolresult_shapes() {
    // ②-b (A)：ToolResult.to_dict() 信封 {success, output} → data=output
    let ok =
        normalize_mcp_tool_result(json!({"success": true, "output": {"rows": 3}}), "t1").unwrap();
    assert!(ok.success);
    assert_eq!(ok.data, json!({"rows": 3}));

    // ②-b (B)：解包直返的业务 dict 恰带 success 键 → data=inner
    let ok2 =
        normalize_mcp_tool_result(json!({"success": true, "memory_id": "m-1"}), "t1").unwrap();
    assert!(ok2.success);
    assert_eq!(ok2.data, json!({"success": true, "memory_id": "m-1"}));

    // ②-b success=false → failure(error)
    let fail = normalize_mcp_tool_result(json!({"success": false, "error": "boom"}), "t1").unwrap();
    assert!(!fail.success);
    assert_eq!(fail.error.as_deref(), Some("boom"));
}
#[test]
fn test_normalize_mcp_result_error_and_plain_data_shapes() {
    // ③ 纯业务数据 → success(data=inner)。顶层 error 键**不是**失败信号——
    // 业务载荷的 error 字段（如 task_manage get 详情返回的下级任务失败原因）
    // 与执行失败信封必须可区分，否则成功查询被翻转成工具报错（事故
    // 2026-09-28：废除按结果文本猜失败的兼容分支，成败只认显式 success 键）。
    let plain = normalize_mcp_tool_result(json!({"error": "payload-level error"}), "t1").unwrap();
    assert!(plain.success, "无 success 键的载荷不得因 error 键被判失败");
    assert_eq!(plain.data, json!({"error": "payload-level error"}));

    let plain2 = normalize_mcp_tool_result(json!({"result": [1, 2]}), "t1").unwrap();
    assert!(plain2.success);
    assert_eq!(plain2.data, json!({"result": [1, 2]}));

    // MCP isError=true 的协议层失败：extract 提取点已显式转 {success, error}
    // （extract_mcp_content），走 ②-b 失败分支。
    let fail =
        normalize_mcp_tool_result(json!({"success": false, "error": "isError"}), "t1").unwrap();
    assert!(!fail.success);
    assert_eq!(fail.error.as_deref(), Some("isError"));
}
#[test]
fn test_normalize_mcp_result_metadata_passthrough() {
    // ②-a 真 ToolExecutionResult 信封：顶层 metadata 透传（task_evaluation_completed
    // / task_failed 等副作用信号的唯一载体，tool_core 侧效派生消费）。
    let ok = normalize_mcp_tool_result(
        json!({
            "success": true,
            "data": {"overall_passed": true, "task_id": "t1"},
            "metadata": {"action": "auto_complete", "result": "completed"},
        }),
        "task_evaluate",
    )
    .unwrap();
    assert!(ok.success);
    assert_eq!(ok.metadata.as_ref().unwrap()["result"], "completed");

    // ②-b：output 信封顶层 metadata 同样保留
    let ok2 = normalize_mcp_tool_result(
        json!({"success": true, "output": {"rows": 3}, "metadata": {"task_failed": true}}),
        "t1",
    )
    .unwrap();
    assert_eq!(ok2.metadata.as_ref().unwrap()["task_failed"], true);

    // ③ 纯业务数据无 metadata → None（不伪造）
    let plain = normalize_mcp_tool_result(json!({"result": [1, 2]}), "t1").unwrap();
    assert!(plain.metadata.is_none());
}
#[test]
fn test_normalize_mcp_result_non_boolean_success_is_parse_error() {
    // K7：success 键存在但非 bool（字符串/整数状态码等信封漂移）→ PARSE_ERROR，
    // 不得 unwrap_or(true) 把失败包装成成功污染下游 state。
    for bad in [
        json!({"success": "true"}),
        json!({"success": 0}),
        json!({"success": "ok", "output": {}}),
    ] {
        let err = normalize_mcp_tool_result(bad, "drifting_tool").unwrap_err();
        assert_eq!(
            err.code.as_deref(),
            Some("PARSE_ERROR"),
            "非布尔 success 必须 PARSE_ERROR: {err}"
        );
        assert!(
            err.message.contains("non-boolean"),
            "报错应指明信封漂移: {err}"
        );
    }
}
#[test]
fn test_extract_mcp_content_normal_response() {
    let inner_json = r#"{"state_updates":{"key":"value"}}"#;
    let mcp_result = json!({
        "content": [{"type": "text", "text": inner_json}],
        "isError": false
    });
    let extracted = extract_mcp_content(&mcp_result);
    assert_eq!(extracted["state_updates"]["key"], "value");
}
#[test]
fn test_extract_mcp_content_is_error() {
    let mcp_result = json!({
        "content": [{"type": "text", "text": "something went wrong"}],
        "isError": true
    });
    let extracted = extract_mcp_content(&mcp_result);
    // 协议层失败在提取点显式转 {success: false, error}（归一化只认显式 success）
    assert_eq!(extracted["success"], false);
    assert_eq!(extracted["error"], "something went wrong");
}
#[test]
fn test_extract_mcp_content_empty_content_array() {
    let mcp_result = json!({
        "content": [],
        "isError": false
    });
    let extracted = extract_mcp_content(&mcp_result);
    // 标准 MCP 协议归一：空 content → 空 output 成功信封（不再裸透原始信封）
    assert_eq!(extracted["success"], true);
    assert_eq!(extracted["output"], json!({}));
}
#[test]
fn test_extract_mcp_content_text_not_json() {
    let mcp_result = json!({
        "content": [{"type": "text", "text": "not_a_json_string"}],
        "isError": false
    });
    let extracted = extract_mcp_content(&mcp_result);
    // 标准 MCP 协议归一：非 JSON text 视为普通文本结果（不再裸透原始信封）
    assert_eq!(extracted["success"], true);
    assert_eq!(extracted["output"]["text"], "not_a_json_string");
}
#[test]
fn test_extract_mcp_content_missing_content_field() {
    let mcp_result = json!({"isError": false});
    let extracted = extract_mcp_content(&mcp_result);
    // 无 content 字段 → fallback 到 clone 原对象
    assert_eq!(extracted["isError"], false);
}
#[test]
fn test_extract_mcp_content_standard_image_converted_to_images_channel() {
    // 标准 MCP 多模态字段 → 内部 images 通道（inject_multimodal 来源 2）
    let mcp_result = json!({
        "content": [
            {"type": "text", "text": "Screenshot captured."},
            {"type": "image", "data": "QUJD", "mimeType": "image/png"}
        ],
        "isError": false
    });
    let extracted = extract_mcp_content(&mcp_result);
    assert_eq!(extracted["success"], true);
    assert_eq!(extracted["output"]["text"], "Screenshot captured.");
    let images = extracted["output"]["images"].as_array().unwrap();
    assert_eq!(images.len(), 1);
    assert_eq!(images[0]["base64"], "QUJD");
    assert_eq!(images[0]["mime_type"], "image/png");
}
#[test]
fn test_extract_mcp_content_image_only() {
    let mcp_result = json!({
        "content": [{"type": "image", "data": "WFla", "mimeType": "image/jpeg"}],
        "isError": false
    });
    let extracted = extract_mcp_content(&mcp_result);
    let images = extracted["output"]["images"].as_array().unwrap();
    assert_eq!(images.len(), 1);
    assert_eq!(images[0]["mime_type"], "image/jpeg");
    assert!(extracted["output"].get("text").is_none());
}
#[test]
fn test_extract_mcp_content_resource_text_joined_with_text() {
    let mcp_result = json!({
        "content": [
            {"type": "text", "text": "line1"},
            {"type": "resource", "resource": {"text": "res-body"}}
        ],
        "isError": false
    });
    let extracted = extract_mcp_content(&mcp_result);
    assert_eq!(
        extracted["output"]["text"],
        "line1
res-body"
    );
}
#[test]
fn test_extract_mcp_content_internal_json_with_image_stays_standard() {
    // content[0].text 是内部 JSON 但混有 image 项 → 仍走标准转换
    // （内部约定永不携带 image 项；出现即视为标准 MCP 多段 content）
    let mcp_result = json!({
        "content": [
            {"type": "text", "text": "{\"a\": 1}"},
            {"type": "image", "data": "QQ==", "mimeType": "image/png"}
        ],
        "isError": false
    });
    let extracted = extract_mcp_content(&mcp_result);
    assert_eq!(extracted["output"]["text"], "{\"a\": 1}");
    assert_eq!(extracted["output"]["images"].as_array().unwrap().len(), 1);
}
/// P6：sidecar pipeline 插件缺 invoke_entry 时，invoke_pipeline_plugin 返回
/// 明确的 MISSING_INVOKE_ENTRY 错误（不再静默回退字面量 "execute"）。
/// 此为运行期防线；启动期聚合校验（plugin-loader discover）是主门。
#[tokio::test]
async fn test_invoke_pipeline_plugin_missing_invoke_entry_returns_error() {
    let loader = Arc::new(MockLoader::new());
    // 缺 invoke_entry 的 sidecar pipeline 插件
    loader.add_manifest(make_pipeline_sidecar_manifest("bad_pipeline", None));

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

    let result = invoker.invoke_pipeline_plugin("bad_pipeline", &ctx).await;
    assert!(result.is_err(), "missing invoke_entry must error");
    let err = result.unwrap_err();
    assert_eq!(
        err.code.as_deref(),
        Some("MISSING_INVOKE_ENTRY"),
        "error code must be MISSING_INVOKE_ENTRY, got: {:?}",
        err.code
    );
    assert!(
        err.message.contains("bad_pipeline"),
        "error message must name the offending plugin: {}",
        err.message
    );
}
/// W2b：sidecar 路径 state.reads 切片投喂的调用面覆盖。
///
/// 注入未连接的 HTTP 客户端进缓存（HTTP transport 不判死，fast path 直复用，
/// 无需真实服务端），invoke_pipeline_plugin 走到 arguments_json 构造（含
/// state_feed_payload 切片组装，声明/未声明两分支）后 call_tool_raw 对
/// 不可达端口失败——MCP_CALL_FAILED 证明构造段已执行且不 panic。
#[tokio::test]
async fn test_sidecar_pipeline_state_reads_slice_reaches_arguments_construction() {
    for (plugin_id, with_reads) in [("slice_declared", true), ("slice_undeclared", false)] {
        let loader = Arc::new(MockLoader::new());
        let mut manifest = make_http_manifest(plugin_id, "http://127.0.0.1:9/mcp");
        manifest.plugin_type = PluginType::Pipeline;
        manifest.invoke_entry = Some(format!("{plugin_id}.execute"));
        if with_reads {
            manifest.state = Some(agentos_core::traits::StateDeclaration {
                reads: vec!["messages_tail:1".to_string(), "task.status".to_string()],
                ..Default::default()
            });
        }
        loader.add_manifest(manifest);
        let invoker = PluginInvokerImpl::new(loader);
        // 注入未连接 HTTP 客户端（与连接后同构：child 恒 None → 不判死）。
        invoker.mcp_clients.write().insert(
            format!("plugin:{plugin_id}"),
            Arc::new(tokio::sync::RwLock::new(McpClient::new_http(
                "http://127.0.0.1:9/mcp",
                HashMap::new(),
                None,
            ))),
        );

        let state = json!({
            "messages": [{"role": "user"}, {"role": "assistant"}],
            "task.status": "running",
            "pipeline_id": "p-1",
            "undeclared_big": "should-not-panic-either-way",
        });
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

        let result = invoker.invoke_pipeline_plugin(plugin_id, &ctx).await;
        let err = result.expect_err("不可达 HTTP 端点必须失败");
        assert_eq!(
            err.code.as_deref(),
            Some("MCP_CALL_FAILED"),
            "{plugin_id}: 应到达 arguments 构造后在 call_tool_raw 失败，got: {err:?}"
        );
    }
}
