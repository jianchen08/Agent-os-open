//! 装配注入与生命周期事件旁路：router/hook_bus 注入、停机 drain、disable 窄口
//! kill、预热、崩溃回调与 OnError fan-out。
//!
//! 自 invoker.rs 按关注点纯搬运拆出（2026-10-12 文件长度机械闸首个应用案例，
//! ADR docs/decisions/2026-10-12-file-length-mechanical-gate.md）。

use std::time::SystemTime;

use agentos_hooks::{EventTarget, LifecycleEvent};
use serde_json::json;

use super::*;

/// 成员下线回调面（on_cohost_member_offline 注册 / observer 快照转发共用形态）。
type MemberOfflineCallback = Arc<dyn Fn(&MemberOfflineNotice) + Send + Sync>;

impl PluginInvokerImpl {
    /// 设置 Capability 路由器（启用 sidecar→内核反向调用）。
    ///
    /// 必须在 engine 创建后调用（路由器需要 engine 句柄）。
    /// 之后新建的 MCP 客户端会自动带上路由器；**已缓存的客户端直接废弃**
    /// （kill + 移除，下次调用 respawn）——缓存命中路径不会重连，若保留
    /// 旧实例，router 前 spawn 的 sidecar（如 boot 期 G2 存量校验窗口，
    /// 见 agentos-kernel.rs 注册闸 G2 块与 set_router 的顺序）将以空
    /// capabilities initialize 长存，插件反向调用（tool-executor/
    /// service-registry）永远 KeyError。
    pub fn set_router(&self, router: Arc<dyn CapabilityRouter>) {
        // sidecar：存 router，新建 MCP client 时带上（PluginScopedRouter）。
        // native：router 存此，execute 时包成 NativeHostServices 注入 ExecContext
        // （host 调 capability 经其走 router.handle）。
        // 一行接通两种 host_type 的 capability 反向调用。
        *self.router.write() = Some(router);

        // 废弃全部已缓存 sidecar：它们 initialize 时拿到的 capabilities 集合
        // 是旧 router 状态（可能为空）的快照，且永不重连。
        self.invalidate_all_sidecars("set_router: cached sidecars hold stale router snapshot");
    }

    /// 全量废弃已缓存 sidecar 宿主（缺陷E，台账 R2 boot 竞态收口原语）：
    /// kill + drain 缓存 + 清 spawn 成员集快照与待重加入集——下次调用 respawn，
    /// initialize 按 router 当前 known_namespaces 重新声明。
    ///
    /// 使用点：①set_router（旧 router 快照的缓存客户端必须废弃）；②boot 末期
    /// chat.send_message 等内核 capability 注册完成后（注册可能晚于部分宿主的
    /// initialize 握手——G2 校验/预热宿主的声明面快照缺新 namespace，宿主进程
    /// 内成员热重载还会重放该陈旧快照；注册后废弃一次，全部宿主按新声明面
    /// 重生）。
    ///
    /// kill 是 best-effort（失败仅 debug 日志——respawn 时 is_alive 会走崩溃
    /// 清理兜底）；同步上下文（无 tokio runtime 的测试等）无法 spawn 异步 kill：
    /// 缓存已清空（下次调用必然 respawn 新实例），旧进程交由 stdio 关闭/
    /// idle GC 兜底，宁可短暂冗余也不 panic。
    pub fn invalidate_all_sidecars(&self, reason: &str) {
        let invalidated: Vec<(String, Arc<tokio::sync::RwLock<McpClient>>)> =
            self.mcp_clients.write().drain().collect();
        if invalidated.is_empty() {
            return;
        }
        tracing::info!(
            reason = reason,
            count = invalidated.len(),
            "invalidate_all_sidecars: 全量废弃缓存宿主（下次调用按当前声明面重握手）"
        );
        // spawn 成员集快照与缓存条目同生命周期：全量驱逐一并清空（respawn 重记）
        self.spawned_members.write().clear();
        self.pending_rejoin_members.write().clear();
        // Job 守卫随记录全量释放（kill-on-close 兜底整树；下方 fire-and-forget
        // kill 对已被 Job 关闭终结的树成为 no-op）
        self.clear_host_jobs();
        let kill = async move {
            for (id, client) in invalidated {
                if let Err(e) = client.write().await.kill().await {
                    tracing::debug!(
                        "invalidate_all_sidecars: best-effort kill of sidecar {id} failed: {e}"
                    );
                }
            }
        };
        match tokio::runtime::Handle::try_current() {
            Ok(handle) => {
                handle.spawn(kill);
            }
            Err(_) => {
                tracing::debug!(
                    "invalidate_all_sidecars: no tokio runtime, cached sidecars dropped without kill"
                );
            }
        }
    }

    /// 内核停机口：drain 全部 `mcp_clients` 并逐个 kill（0.2 收尾 §3.3a）。
    ///
    /// 直接仿 [`set_router`] 的 drain+kill 既有模式；差异在两点：
    /// - **await 内联**：停机路径进程即将退出，不存在 respawn 竞态窗口，无需
    ///   fire-and-forget——调用方（graceful shutdown / exit 75 排空）等 kill
    ///   完成再退出，保证零孤儿；
    /// - **逐 kill 超时保护**：每个 kill（含写锁获取）上限 2s——个别 sidecar
    ///   卡死（管道阻塞 / kill 挂起）不得拖住退出路径（best-effort，超时记
    ///   warn 后继续下一个）。
    ///
    /// 空（或已 drain 空）缓存 no-op。
    pub async fn shutdown_all(&self) {
        let drained: Vec<(String, Arc<tokio::sync::RwLock<McpClient>>)> =
            self.mcp_clients.write().drain().collect();
        if drained.is_empty() {
            return;
        }
        // spawn 成员集快照与缓存条目同生命周期：停机 drain 一并清空
        self.spawned_members.write().clear();
        self.pending_rejoin_members.write().clear();
        // Job 守卫随记录全量释放（kill-on-close 兜底整树——显式 kill 超时/
        // 失败的残留进程由此收尾，内核退出前的最后防线）
        self.clear_host_jobs();
        let total = drained.len();
        for (id, client) in drained {
            // 超时盖住整个「取写锁 + kill」——写锁死锁与 kill 挂起同等会卡退出。
            let kill_fut = async { client.write().await.kill().await };
            match tokio::time::timeout(Duration::from_secs(2), kill_fut).await {
                Ok(Ok(())) => {}
                Ok(Err(e)) => {
                    warn!("shutdown_all: best-effort kill of sidecar {id} failed: {e}")
                }
                Err(_) => {
                    warn!("shutdown_all: kill of sidecar {id} timed out (2s), skipping")
                }
            }
        }
        info!("shutdown_all: drained and killed {total} cached sidecar(s)");
    }

    /// disable 窄口 kill（0.2 收尾 §3.3b）：`mcp_clients` 移除该插件条目，有则
    /// kill，无则 no-op。
    ///
    /// 刻意**不走 [`Self::force_unload`]**：它含 OnUnload 事件广播 +
    /// loader.unload + 指纹清理，对"仅禁用"语义过重（插件仍在 loader 内，热
    /// 发现不失效）；sidecar 本就按调用懒 spawn，kill 后 reenable 下次调用
    /// 自然重生。kill（含写锁获取）带 2s 超时保护，防个别卡死阻塞 HTTP
    /// handler。幂等：缓存无条目（从未 spawn / 已是 HTTP 无子进程 / 重复调用）
    /// 直接返回。
    pub async fn kill_sidecar_if_any(&self, plugin_id: &str) {
        // 宿主粒度：light 成员 disable 连坐整组（其他成员下次调用 respawn，
        // 分配表条目保留——reenable 后回到原宿主）。
        let Some(host_key) = self.existing_host_key_for(plugin_id) else {
            return;
        };
        let Some(client) = self.mcp_clients.write().remove(&host_key) else {
            return;
        };
        // spawn 成员集快照与缓存条目同生命周期：kill 即清（respawn 重记）
        self.spawned_members.write().remove(&host_key);
        self.pending_rejoin_members.write().remove(&host_key);
        // Job 守卫随记录释放（kill-on-close 兜底整树：显式 kill 失败/超时的
        // 残留进程由此收尾，见 host_jobs 字段注释）
        self.release_host_job(&host_key);
        let kill_fut = async { client.write().await.kill().await };
        match tokio::time::timeout(Duration::from_secs(2), kill_fut).await {
            Ok(Ok(())) => {
                info!("kill_sidecar_if_any: killed host {host_key} of disabled plugin {plugin_id}")
            }
            Ok(Err(e)) => {
                warn!("kill_sidecar_if_any: best-effort kill of {host_key} failed: {e}")
            }
            Err(_) => {
                warn!("kill_sidecar_if_any: kill of {plugin_id} timed out (2s)")
            }
        }
    }

    /// 注入生命周期钩子事件总线（旁路广播，可选）。
    ///
    /// 镜像 [`set_router`] 的 `&self` 注入形态：内核 main 在创建总线后、spawn 任何
    /// sidecar 前调用，把同一 `Arc<HookEventBus>` 注入 invoker，使后续 sidecar spawn
    /// 的 OnLoad 等生命周期事件在点对点直调**旁路** fan-out 给审计/指标订阅者。
    /// 未调用时（`None`）行为完全不变——直调仍是唯一路径。
    pub fn set_hook_bus(&self, bus: Arc<HookEventBus>) {
        *self.hook_bus.write() = Some(bus);
    }

    /// 注入内存预算闸回调（ADR 2026-10-19 D5 闸面；内核 main 启动期装配）。
    ///
    /// 采样闭包由装配方提供——内核 Private Bytes 采样函数在 bin 层，经闭包进入
    /// invoker，crate 依赖方向不反转（`with_restart_hook` 注入先例同构）。同一
    /// 时刻至多一个闸（后注入覆盖前注入）；未注入（默认）= 预算未启用，恒直通。
    pub fn set_pre_spawn_gate(&self, gate: PreSpawnGate) {
        *self.pre_spawn_gate.write() = Some(gate);
    }

    /// 当前预算闸快照（spawn 慢路径准入检查用；`None` = 未启用直通）。
    /// 克隆 Arc 出读锁，检查期间不持锁。
    pub(super) fn pre_spawn_gate(&self) -> Option<PreSpawnGate> {
        self.pre_spawn_gate.read().clone()
    }

    /// 预热插件宿主：确保 sidecar 已 spawn 并完成 initialize 握手（幂等——
    /// 缓存命中直接返回，不重 spawn；single-flight 语义与正常调用路径同一把
    /// per-host 锁，与在途调用并发安全）。
    ///
    /// boot 后台预热用：把"启动后首次调用"的宿主冷启动（spawn→MCP initialize
    /// 每宿主秒级）提前到用户尚未发消息的窗口。**不执行任何插件逻辑**——
    /// 仅建连；native（InProcess）无进程模型，no-op Ok。预热集插件登记进
    /// keep-warm 常驻集：其所在宿主组豁免 idle GC（预热常驻——预热若仍被
    /// 空闲回收架空，新会话首条消息会重新全价冷启动，预热失去意义）。非预热
    /// 插件照常懒 spawn + idle GC 治理。失败返回 Err 交调用方记日志跳过
    /// （常驻登记照常生效：spawn 失败的宿主组豁免回收，懒 spawn 兜底补齐）。
    pub async fn warmup_sidecar(&self, manifest: &PluginManifest) -> Result<(), PluginError> {
        if manifest.host_type != HostType::Sidecar {
            return Ok(());
        }
        self.keep_warm_plugins.write().insert(manifest.id.clone());
        self.get_or_create_mcp_client(manifest).await.map(|_| ())
    }

    /// 插件源码目录解析（watcher G2 复验用）：复用 loader 的发现结果拿插件根
    /// 目录。未发现（未装载/已卸载）返回 None。
    pub fn plugin_source_dir(&self, plugin_id: &str) -> Option<std::path::PathBuf> {
        self.loader
            .get_plugin_dir(plugin_id)
            .map(std::path::PathBuf::from)
    }

    /// 预分配宿主键（纯内存，无 IO 无 spawn）：light 成员写入装箱分配表，
    /// solo 成员仅构造键（无状态）。
    ///
    /// 批量预热**必须先逐个预分配、再并发 warmup**：合宿宿主 spawn 的
    /// `--members` 与组指纹取自分配表实时快照——若边分配边 spawn，先到的
    /// 成员会把宿主以"单成员集"spawn 定型，后到成员在指纹 TTL（1s）内被
    /// 快速路径短路复用，宿主里实际没有该成员（预热假成功）。预分配先行
    /// 让每个组宿主一次 spawn 即装载完整成员集，组指纹一次定型零 respawn。
    pub fn preassign_host_key(&self, manifest: &PluginManifest) {
        self.resolve_host_key(manifest);
    }

    /// 旁路广播 OnError 给总线订阅者（审计 / `lifecycle.plugin_error_total` 计数）。
    ///
    /// 与 OnLoad 的旁路 emit 对称：插件调用**失败**时由调用方（[`invoke_pipeline_plugin`]
    /// / [`invoke_tool`] 的中央返回处）调一次本方法，把"插件调用失败"这一事实
    /// fan-out 给观察层。失败两种形态都计：调用层 `Err`（崩溃/MCP/解析）与
    /// Ok 携带失败结果（pipeline error 字段 / tool success=false）。
    /// **不改错误处理语义**——原错误/结果照常向上传播，本方法仅 fire-and-forget
    /// 观察一次。
    ///
    /// best-effort、非阻塞：未注入总线（`None`，如单测）时 no-op，行为不变。
    pub(super) fn emit_lifecycle_error(&self, plugin_id: &str, message: &str, code: Option<&str>) {
        let bus_guard = self.hook_bus.read();
        let Some(bus) = bus_guard.as_ref() else {
            return;
        };
        let mut ctx = HookContext::new();
        ctx.set("plugin_id", json!(plugin_id));
        ctx.set("error", json!(message));
        if let Some(code) = code {
            ctx.set("error_code", json!(code));
        }
        bus.emit(LifecycleEvent {
            hook: LifecycleHook::OnError,
            ctx,
            target: EventTarget::Plugin(plugin_id.to_string()),
            ts: SystemTime::now(),
        });
    }

    /// 注册崩溃回调。
    pub fn on_crash(&self, callback: Arc<dyn Fn(&str) + Send + Sync>) {
        self.crash_callbacks.write().push(callback);
    }

    /// 通知崩溃回调。
    pub(super) fn notify_crash(&self, plugin_id: &str) {
        let callbacks = self.crash_callbacks.read();
        for cb in callbacks.iter() {
            cb(plugin_id);
        }
    }

    /// 注册 spawn 熔断跳闸回调（卡2/卡3：熔断 = 判定「宿主当前不可用」，
    /// 内核据此发系统通知）。跳闸恰好一次，回调恰好一次。
    pub fn on_spawn_breaker(&self, callback: Arc<dyn Fn(&SpawnBreakerNotice) + Send + Sync>) {
        self.spawn_breaker_callbacks.write().push(callback);
    }

    /// 通知 spawn 熔断跳闸（best-effort，回调异常不下毒记账路径）。
    pub(super) fn notify_spawn_breaker(&self, notice: &SpawnBreakerNotice) {
        for cb in self.spawn_breaker_callbacks.read().iter() {
            cb(notice);
        }
    }

    /// 注册 spawn 恢复回调（卡3 恢复沿：熔断宿主被成功收束 → kind=restored
    /// 系统通知）。恰好一次（record_success 返回 true 时）。
    pub fn on_spawn_recovered(&self, callback: Arc<dyn Fn(&SpawnRecoveredNotice) + Send + Sync>) {
        self.spawn_recovered_callbacks.write().push(callback);
    }

    /// 通知 spawn 恢复（best-effort，回调异常不下毒记账路径）。
    pub(super) fn notify_spawn_recovered(&self, notice: &SpawnRecoveredNotice) {
        for cb in self.spawn_recovered_callbacks.read().iter() {
            cb(notice);
        }
    }

    /// 注册合宿成员下线回调（卡3：host.py fail-soft 结构性行 → 系统通知）。
    pub fn on_cohost_member_offline(&self, callback: MemberOfflineCallback) {
        self.member_offline_callbacks.write().push(callback);
    }

    /// 注册到新客户端的成员下线观察者：快照当前回调列表，逐个转发。
    pub(super) fn member_offline_observer(&self) -> Option<MemberOfflineCallback> {
        let callbacks = self.member_offline_callbacks.read();
        if callbacks.is_empty() {
            return None;
        }
        let snapshot: Vec<_> = callbacks.iter().cloned().collect();
        Some(Arc::new(move |notice: &MemberOfflineNotice| {
            for cb in &snapshot {
                cb(notice);
            }
        }))
    }
}
