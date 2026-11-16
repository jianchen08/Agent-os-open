//! 崩溃恢复族：死亡判定、假死评估与冻结取证、可恢复错误分类与透明恢复
//! （respawn + 重试一次）。
//!
//! 自 invoker.rs 按关注点纯搬运拆出（2026-10-12 文件长度机械闸首个应用案例，
//! ADR docs/decisions/2026-10-12-file-length-mechanical-gate.md）。

use super::*;

impl PluginInvokerImpl {
    /// 崩溃回调：调用失败且为 panic/fatal 时通知（native pipeline/tool 共用，
    /// 泛型 T——只看 Err 侧错误码，pipeline 与 tool 两种结果类型通用）。
    pub(super) fn notify_if_crash<T>(&self, plugin_id: &str, result: &Result<T, PluginError>) {
        if let Err(ref e) = result {
            if e.code
                .as_deref()
                .map(|c| c.contains("PANICKED") || c.contains("FATAL"))
                .unwrap_or(false)
            {
                self.notify_crash(plugin_id);
            }
        }
    }

    /// B1（M2-reactive 第一刀）：判定 MCP 客户端是否为「已死亡的 stdio sidecar 进程」。
    ///
    /// 委托 [`McpClient::is_dead`] 三证据判定：stdio 写失败标记（进程死亡铁证，
    /// 弥补 try_wait 快照在退出通知未到/被强杀时的竞态盲区）/ child 缺失
    /// （kill 后残留缓存）/ 退出快照。HTTP transport 不判死（远端进程不在
    /// 本地，网络错误走调用侧错误语义）。
    pub(super) async fn is_dead_sidecar(client: &McpClient) -> bool {
        // 委托 client 自身状态：stdio 写失败标记 / child 缺失（kill 后残留缓存）
        // / 退出快照 三证据（见 McpClient::is_dead）。HTTP 运输恒 false。
        client.is_dead().await
    }

    /// 假死判定与取证（进程治理三信号模型的执行端，评估+取证阶段）。
    ///
    /// 调用时机：call_tool 失败且进程存活（[`Self::is_dead_sidecar`] 已排除
    /// 真死亡）。三条件交叉判定假死：
    /// ① 进程存活（try_wait 无退出——调用方已查）；
    /// ② 事件循环心跳停跳超过 FREEZE_HEARTBEAT_STALE_SECS（SDK 心跳协程
    ///    跑在 sidecar 主事件循环，冻结即停跳；从未上报心跳的旧版本 sidecar
    ///    返回 None → 不判定，平滑兼容）；
    /// ③ 本次调用失败（调用时机已保证——本函数只在 call_tool Err 分支进入）。
    ///
    /// 命中即做冻结取证：判定命中时刻正是冻结窗口，respawn 抹掉现场之前抓
    /// py-spy 线程栈（熔断停手分支同样抓——反复冻结是最需要现场的时刻）。
    ///
    /// 返回 `true` 表示应触发 force_unload 自愈（与 idle-unload 同一回收路径：
    /// kill 进程树 + 驱逐客户端缓存，下次调用自动 respawn 干净实例），调用
    /// 本身仍以 MCP_CALL_FAILED 返回交由既有恢复包装器处理。
    /// 熔断：10 分钟窗口 >= 3 次（FREEZE_RESPAWN_WINDOW_SECS / MAX）停手只
    /// 取证告警，防循环重启掩盖根因（同构 K8s CrashLoopBackOff）。
    ///
    /// 锁契约：本函数只做判定与取证，**不执行 unload**——unload 内部对同一
    /// client Arc 取写锁（[`Self::unload_host`]），调用方读锁存活时执行 =
    /// 同任务自等写锁永久悬挂。调用方必须先释放 `client` 读锁再触发回收
    /// （见 [`Self::attempt_sidecar_pipeline`] 的失败分支）。
    pub(super) async fn evaluate_frozen_sidecar(
        &self,
        client: &McpClient,
        plugin_id: &str,
        call_err_message: &str,
    ) -> bool {
        const FREEZE_HEARTBEAT_STALE_SECS: u64 = 30;
        const FREEZE_RESPAWN_WINDOW_SECS: u64 = 600;
        const FREEZE_RESPAWN_MAX: usize = 3;
        const FREEZE_DUMP_TIMEOUT: u64 = 10;

        let Some(age) = client.heartbeat_age_secs() else {
            return false; // 旧版本 sidecar / HTTP transport：无心跳数据，兼容期不判死
        };
        if age <= FREEZE_HEARTBEAT_STALE_SECS {
            return false; // 心跳新鲜：非假死（协议/工具错误走既有语义）
        }

        // 熔断检查（滑窗计数）
        let now = Instant::now();
        let circuit_broken = {
            let mut ledger = self.freeze_respawns.write();
            let entries = ledger.entry(plugin_id.to_string()).or_default();
            entries.retain(|t| now.duration_since(*t).as_secs() < FREEZE_RESPAWN_WINDOW_SECS);
            if entries.len() >= FREEZE_RESPAWN_MAX {
                tracing::error!(
                    target: "sidecar",
                    "[{}] 疑似事件循环冻结（心跳停跳 {}s）但 10 分钟内已假死 respawn {} 次，熔断停手——人工介入排查根因",
                    plugin_id, age, entries.len()
                );
                true
            } else {
                entries.push(now);
                false
            }
        };

        // 冻结现场取证：判定命中时刻正是冻结窗口，respawn 抹掉现场之前抓
        // py-spy 线程栈（熔断停手分支同样抓——反复冻结是最需要现场的时刻）。
        let dump_dir = std::path::PathBuf::from("logs").join("freeze_dumps");
        let spy_bin = std::env::var("AGENTOS_PY_SPY_BIN").unwrap_or_else(|_| "py-spy".to_string());
        match client.pid().await {
            Some(pid) => {
                Self::run_freeze_dump(
                    &spy_bin,
                    plugin_id,
                    pid,
                    age,
                    &dump_dir,
                    FREEZE_DUMP_TIMEOUT,
                )
                .await;
            }
            None => {
                tracing::warn!(target: "sidecar", "[{}] 冻结取证跳过：无进程 pid", plugin_id);
            }
        }
        if circuit_broken {
            return false;
        }

        tracing::warn!(
            target: "sidecar",
            "[{}] 疑似事件循环冻结：进程存活但心跳停跳 {}s（阈值 {}s），本次调用失败: {} —— Force unload 触发自愈 respawn",
            plugin_id, age, FREEZE_HEARTBEAT_STALE_SECS, call_err_message
        );
        true
    }

    /// 冻结现场取证：对疑似冻结的 sidecar 进程执行 py-spy dump 抓线程栈。
    ///
    /// 产物落 `dir/{plugin_id}_{时间戳}_pid{pid}.txt`（头部带 plugin/pid/
    /// 心跳停跳时长元信息，正文为 py-spy stdout/stderr）。取证是尽力而为：
    /// py-spy 缺失/超时只降级告警返回 None，绝不 panic/长阻塞——调用点
    /// （[`Self::evaluate_frozen_sidecar`]）评估完成后调用方要做 force_unload
    /// 自愈，回收是硬时限。二进制由调用方解析（默认 PATH 上的 `py-spy`，可用
    /// `AGENTOS_PY_SPY_BIN` 覆盖，venv 部署 PATH 不含 Scripts 目录的场景）。
    /// D3：freeze 取证 dump 保留上限（份）——取证场景无法预知何时需要现场，
    /// 保留最新 10 份 + 写入时顺带清扫，防 logs/freeze_dumps 随冻结次数无界增长。
    const FREEZE_DUMP_KEEP: usize = 10;

    /// 清扫 freeze 取证目录只留最新 `keep` 份（按文件写时间升序删最旧）。
    /// 跨插件共用一个总上限（取证无法预知哪个插件需要现场）；best-effort：
    /// 枚举失败静默返回（目录不存在 = 无可清扫），单个删除失败 warn 留痕继续。
    pub(super) fn sweep_freeze_dump_dir(dir: &std::path::Path, keep: usize) {
        let Ok(entries) = std::fs::read_dir(dir) else {
            return;
        };
        let mut files: Vec<(std::time::SystemTime, std::path::PathBuf)> = entries
            .filter_map(|e| e.ok())
            .filter(|e| e.path().is_file())
            .filter_map(|e| {
                let modified = e.metadata().ok()?.modified().ok()?;
                Some((modified, e.path()))
            })
            .collect();
        if files.len() <= keep {
            return;
        }
        let excess = files.len() - keep;
        files.sort();
        for (modified, path) in files.into_iter().take(excess) {
            if let Err(e) = std::fs::remove_file(&path) {
                tracing::warn!(
                    target: "sidecar",
                    "freeze 取证清扫失败 {}: {}（mtime {:?}）",
                    path.display(),
                    e,
                    modified
                );
            }
        }
    }

    pub(super) async fn run_freeze_dump(
        spy_bin: &str,
        plugin_id: &str,
        pid: u32,
        heartbeat_age_secs: u64,
        dir: &std::path::Path,
        timeout_secs: u64,
    ) -> Option<std::path::PathBuf> {
        if let Err(e) = std::fs::create_dir_all(dir) {
            tracing::warn!(target: "sidecar", "[{}] 冻结取证目录创建失败: {}（跳过抓取）", plugin_id, e);
            return None;
        }
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let path = dir.join(format!("{plugin_id}_{stamp}_pid{pid}.txt"));
        let spawned = tokio::process::Command::new(spy_bin)
            .args(["dump", "--pid", &pid.to_string()])
            .output();
        match tokio::time::timeout(std::time::Duration::from_secs(timeout_secs), spawned).await {
            Ok(Ok(output)) => {
                let report = format!(
                    "# sidecar freeze dump\n# plugin: {plugin_id}\n# pid: {pid}\n\
                     # heartbeat_stale_secs: {heartbeat_age_secs}\n\
                     # captured_at_unix: {stamp}\n\
                     # command: {spy_bin} dump --pid {pid}\n\n--- stdout ---\n{}\n--- stderr ---\n{}\n",
                    String::from_utf8_lossy(&output.stdout),
                    String::from_utf8_lossy(&output.stderr),
                );
                match std::fs::write(&path, report) {
                    Ok(_) => {
                        tracing::info!(
                            target: "sidecar",
                            "[{}] 冻结现场已抓取: {}（py-spy exit={}）",
                            plugin_id,
                            path.display(),
                            output.status
                        );
                        // D3：取证 dump 保留上限——写入时顺带清扫只留最新 10 份
                        Self::sweep_freeze_dump_dir(dir, Self::FREEZE_DUMP_KEEP);
                        Some(path)
                    }
                    Err(e) => {
                        tracing::warn!(
                            target: "sidecar",
                            "[{}] 冻结现场写盘失败 {}: {}",
                            plugin_id,
                            path.display(),
                            e
                        );
                        None
                    }
                }
            }
            Ok(Err(e)) => {
                tracing::warn!(
                    target: "sidecar",
                    "[{}] py-spy（{}）启动失败: {}——冻结现场未抓取（安装 py-spy 或设 AGENTOS_PY_SPY_BIN）",
                    plugin_id,
                    spy_bin,
                    e
                );
                None
            }
            Err(_) => {
                tracing::warn!(
                    target: "sidecar",
                    "[{}] py-spy dump 超时（{}s）——放弃取证继续自愈",
                    plugin_id,
                    timeout_secs
                );
                None
            }
        }
    }

    /// B1：错误是否为「sidecar 死亡/传输断开」——可透明恢复类失败。
    ///
    /// 命中条件：
    /// - `PLUGIN_CRASHED`（attempt 侧经 [`Self::is_dead_sidecar`] 判定）；
    /// - `MCP_CALL_FAILED` 携带 stdio 运输写失败特征（transport error + write/
    ///   flush/broken pipe）——写失败只可能发生在已死进程的句柄上，是死亡铁证；
    ///   既往归为"进程仍存活的协议/工具错误"导致进程死后既不驱逐也不重试
    ///   （与 is_dead_sidecar 的写失败标记互补，双保险）。
    /// - `MCP_TOOL_CALL_FAILED`（协议/工具错误）、`MCP_CONNECT_FAILED`（spawn
    ///   失败，重试同样失败）等不重试。
    pub(super) fn is_recoverable_sidecar_death(err: &PluginError) -> bool {
        if err.code.as_deref() == Some("PLUGIN_CRASHED") {
            return true;
        }
        // call_tool 把底层错误统一包成 "transport error: <write/flush/broken pipe>..."
        // （Windows os error 232 "管道正在被关闭" 等被本地化成消息文本）。
        if err.code.as_deref() == Some("MCP_CALL_FAILED") {
            let msg = err.message.to_lowercase();
            return msg.contains("transport error")
                && (msg.contains("write") || msg.contains("flush") || msg.contains("broken pipe"));
        }
        false
    }

    /// B1（M2-reactive 第一刀）：sidecar 死亡**透明恢复**（brokered transparent
    /// recovery）。
    ///
    /// 计划 §9.4 缺口：长事务在途调用时其依赖 B 被 idle GC 杀掉，现状是错误返回
    /// 而非通知+等待，违反空间可组合性。修复：首次尝试失败且判定目标死亡
    /// （`PLUGIN_CRASHED`，见 [`Self::is_recoverable_sidecar_death`]）时：
    ///
    /// 1. `force_unload_impl` 清缓存（kill 残尸 + 清指纹/last_used + OnUnload 事件）；
    /// 2. attempt 内部经 `get_or_create_mcp_client` 重新 spawn；
    /// 3. **重试一次（仅一次，防循环）**；重试成功 → 对调用方完全透明；
    ///    重试仍失败 → 返回第一次的**原错误**（语义仍是「目标插件崩溃」）。
    ///
    /// 可观测性：重试路径记 info 日志（respawn 标记）；最终失败才 `notify_crash`
    /// （崩溃回调语义保留给「恢复失败」——透明恢复成功时不卸载能力、不记
    /// last_crash_ts，插件实际可用）。
    ///
    /// 这是运行时唯一保留的错误重试行为（ADR 2026-08-18：ErrorPolicy 收敛为
    /// 唯一值 RETRY，manifest 字段已清理，不再产生行为分发）。
    pub(super) async fn with_transparent_recovery<T, F, Fut>(
        &self,
        plugin_id: &str,
        mut attempt: F,
    ) -> Result<T, PluginError>
    where
        F: FnMut() -> Fut,
        Fut: std::future::Future<Output = Result<T, PluginError>>,
    {
        let err = match attempt().await {
            Ok(v) => return Ok(v),
            Err(e) => e,
        };
        if !Self::is_recoverable_sidecar_death(&err) {
            return Err(err);
        }

        // 透明恢复：force_unload 清缓存（best-effort，失败也继续 respawn——
        // get_or_create_mcp_client 自身有「缓存进程已死 → kill+respawn」兜底）。
        info!(
            plugin_id = plugin_id,
            respawn = true,
            "M2-reactive: sidecar died mid-call, transparent recovery (force_unload + respawn + retry once)"
        );
        if let Err(unload_err) = self.force_unload_impl(plugin_id).await {
            warn!(
                plugin_id = plugin_id,
                error = %unload_err.message,
                "transparent recovery: force_unload failed (respawn anyway)"
            );
        }

        match attempt().await {
            Ok(v) => {
                info!(
                    plugin_id = plugin_id,
                    respawn = true,
                    recovered = true,
                    "M2-reactive: transparent recovery succeeded"
                );
                Ok(v)
            }
            Err(retry_err) => {
                warn!(
                    plugin_id = plugin_id,
                    respawn = true,
                    recovered = false,
                    retry_error = %retry_err.message,
                    "M2-reactive: retry after respawn failed, returning original error"
                );
                // 恢复失败 → 保留崩溃回调语义（卸载能力 + 告警 + last_crash_ts）。
                self.notify_crash(plugin_id);
                Err(err)
            }
        }
    }
}
