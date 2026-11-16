//! 卸载族：强制卸载、成员粒度热重载/卸载（宿主进程内定向请求）、整组卸载与
//! Pull 热加载过期判定。
//!
//! 自 invoker.rs 按关注点纯搬运拆出（2026-10-12 文件长度机械闸首个应用案例，
//! ADR docs/decisions/2026-10-12-file-length-mechanical-gate.md）。

use std::time::SystemTime;

use agentos_hooks::{EventTarget, LifecycleEvent};
use serde_json::json;

use super::host_key::{compute_plugin_fingerprint, parse_group_slot, PLUGIN_FINGERPRINT_TTL};

use super::*;

/// 内核 → 宿主的成员粒度热重载请求方法（SDK CohostServer/host.py 注册同名
/// 处理器；带应答，失败以协议错误应答，内核回退 force_unload）。
const RELOAD_MEMBER_METHOD: &str = "agentos/reload_member";

/// 内核 → 宿主的成员粒度卸载请求方法（带应答；ADR
/// 2026-09-24-member-granularity-unload-mode-panel-warmup）：宿主定向 on_unload
/// → 成员表摘除 → 三张聚合表原位重聚合；最后成员以协议错误拒绝，内核对
/// 最后成员走整组回收。失败（旧宿主不认识该方法/协议错误/超时）一律回退
/// 整组驱逐——语义终点与现状一致。
const UNLOAD_MEMBER_METHOD: &str = "agentos/unload_member";
/// 内核 → 宿主的成员加回请求方法（域事件投递前的唤醒原语，ADR 2026-10-28 D5；
/// SDK `CohostServer` 注册同名处理器）。与 reload 的分界：reload 要求成员在
/// 当前成员表里（换码），load 接受「出生名册内已被摘除」的成员（加回）。
/// 幂等——成员已在场时宿主直接应答 `added=false`。
const LOAD_MEMBER_METHOD: &str = "agentos/load_member";

/// reload_member 请求超时上界（控制面操作必须有界）。请求窗口自宿主侧
/// on_load 定向派发落地起覆盖「模块重 exec + on_load 重放」——慢而未死的
/// 成员初始化（慢盘/慢解释器/预热 IO）必须等足预算完成，超时即回退整组
/// 驱逐（fail-closed 不变），而预算过紧会把慢初始化放大成全组陪葬的驱逐
/// 风暴（BUG-19）：30s 明显大于合理慢初始化上界，仍远小于任何挂死宿主的
/// 可容忍驻留。
const RELOAD_MEMBER_TIMEOUT: Duration = Duration::from_secs(30);
/// load_member 请求超时上界：与成员热重载同预算（同一「模块重 exec +
/// on_load 重放」窗口）。唤醒是投递前的前置步骤，超时即放弃本轮唤醒并告警
/// ——绝不阻塞域事件广播主流程。
const LOAD_MEMBER_TIMEOUT: Duration = RELOAD_MEMBER_TIMEOUT;

impl PluginInvokerImpl {
    /// 强制卸载插件的实现（供 trait 方法 force_unload 与内部热重载复用）。
    ///
    /// 宿主语义（合宿进程模型 §4.2）：kill 插件所在**宿主**——light 成员连坐整组
    /// （组指纹变化触发整组 respawn 的对称面），独占成员杀单进程。分配表保留
    /// （respawn 按当前表重建成员集，§4.5 第 2 条分配粘性），槽位回收只在
    /// idle GC 路径发生。
    pub async fn force_unload_impl(&self, plugin_id: &str) -> Result<(), PluginError> {
        // 成员粒度优先（ADR 2026-09-24-member-granularity-unload-mode-panel-warmup）：
        // 存活组宿主的非最后成员先试进程内摘除；失败/不适用回退整组驱逐
        // （语义终点一致：下个调用者拿新码，爆炸半径收缩）。
        if let Some(host_key) = self.existing_host_key_for(plugin_id) {
            if parse_group_slot(&host_key).is_some()
                && self.mcp_clients.read().contains_key(&host_key)
                && self
                    .spawned_members
                    .read()
                    .get(&host_key)
                    .is_some_and(|members| !members.iter().any(|m| m == plugin_id))
            {
                // 软卸载待重加入成员（分配在、宿主存活、快照已知且不含它）：
                // 无进程内服务面可摘，清掉分配标记即完成强制语义（该成员不随
                // 下次 respawn 回归），与「无宿主」路径同一终点——不得为它整组
                // kill。快照未知（None）不进此分支：进程服务面不明按既有
                // fail-closed 路径处理（BUG-44 同哲学）。
                self.light_packing.write().assignments.remove(plugin_id);
                if let Some(pending) = self.pending_rejoin_members.write().get_mut(&host_key) {
                    pending.remove(plugin_id);
                }
                self.emit_lifecycle_unload(plugin_id);
                if let Err(e) = self.loader.unload(plugin_id).await {
                    warn!(
                        "loader unload {} (force unload pending-rejoin member) failed: {}",
                        plugin_id, e
                    );
                }
                return Ok(());
            }
            let group_with_peers =
                parse_group_slot(&host_key).is_some() && self.host_members(&host_key).len() > 1;
            if group_with_peers && self.mcp_clients.read().contains_key(&host_key) {
                match self.unload_group_member(plugin_id).await {
                    Ok(()) => {
                        // 强制语义：撤销「待重加入」分配标记（软卸载保留分配是
                        // idle 语境——成员随下次 respawn 回归；强制卸载要求它
                        // 不再自动回归，见 finalize_member_unload）。
                        self.light_packing.write().assignments.remove(plugin_id);
                        if let Some(pending) =
                            self.pending_rejoin_members.write().get_mut(&host_key)
                        {
                            pending.remove(plugin_id);
                        }
                        return Ok(());
                    }
                    Err(e) => {
                        warn!(
                            "unload_member fallback to whole-host unload for {}: {}",
                            plugin_id, e
                        );
                    }
                }
            }
        }
        self.unload_plugin_host(plugin_id, false).await
    }

    /// 成员粒度热重载实现：向成员所在组宿主发 `agentos/reload_member` 请求
    /// （带应答），宿主进程内重载该成员代码，其余成员进程不动——驱逐爆炸
    /// 半径从整组收缩到单成员（BUG-3 504 的根治面）。
    ///
    /// 三路判定（与 [`PluginInvoker::reload_member`] 契约一致）：
    /// - 独占宿主 → Err（调用方回退 force_unload，单进程杀即等价粒度）；
    /// - 无存活进程（未装箱/未 spawn/已驱逐）→ Ok(no-op)：无进程内代码，
    ///   下次 spawn 必用新码；
    /// - 存活组宿主 → 应答成功 Ok；宿主协议错误/超时 Err（重载失败不破坏
    ///   宿主状态——旧成员仍在服务，回退整组驱逐路径安全）。
    ///
    /// 成功后刷新宿主并集指纹记账：指纹是 spawn 时刻快照，进程内代码已被
    /// 重载为磁盘现码——不刷新会让 pull 路径（host_code_stale）按指纹差误判
    /// stale 而整组 respawn，热重载被架空。
    pub async fn reload_group_member(&self, plugin_id: &str) -> Result<(), PluginError> {
        let Some(host_key) = self.existing_host_key_for(plugin_id) else {
            return Ok(());
        };
        if parse_group_slot(&host_key).is_none() {
            return Err(PluginError {
                message: format!("reload_member: {plugin_id} 不在合宿组宿主（独占无进程内重载面）"),
                code: None,
                source: None,
            });
        }
        let Some(client) = self.mcp_clients.read().get(&host_key).cloned() else {
            return Ok(());
        };
        // 缺陷E（台账 R2）：成员重载必须刷新声明面——请求携带实时 capabilities
        //（与 initialize 同源派生单点 McpClient::declared_capabilities），宿主
        // 换入成员按新声明面注入并刷新留存快照。boot 竞态窗口 spawn 的宿主
        //（握手早于后续 capability 注册，快照缺 namespace）经成员重载即愈，
        // 不再"刷新不粘滞"。
        let declared = client.read().await.declared_capabilities();
        let params = json!({ "plugin_id": plugin_id, "capabilities": declared });
        let outcome = tokio::time::timeout(RELOAD_MEMBER_TIMEOUT, async move {
            client
                .read()
                .await
                .request(RELOAD_MEMBER_METHOD, params)
                .await
        })
        .await;
        match outcome {
            Ok(Ok(result)) => {
                if result.get("reloaded").and_then(Value::as_bool) != Some(true) {
                    return Err(PluginError {
                        message: format!(
                            "reload_member: 宿主应答缺少 reloaded=true（{plugin_id}）"
                        ),
                        code: None,
                        source: None,
                    });
                }
                let fp = self.host_union_fingerprint(&host_key);
                self.fingerprints
                    .write()
                    .insert(host_key.clone(), (fp, Instant::now()));
                // D2：spawn 每成员指纹表同步刷新该成员条目（进程内代码已是磁盘
                // 现码）——pull 路径成员级 diff 以该表为进程内容基准，不刷新会
                // 误报该成员仍需热换（9-15 根因 3 的每成员版）。表缺失（组宿主
                // 不可能有，防御性跳过）不阻断，pull diff 按 fail-closed 回退。
                if let Some(manifest) = self.loader.get_manifest(plugin_id) {
                    let member_fp = self.resolve_fingerprint(&manifest);
                    if let Some(table) = self.host_member_fingerprints.write().get_mut(&host_key) {
                        table.insert(plugin_id.to_string(), member_fp);
                    }
                }
                info!(
                    plugin_id = plugin_id,
                    host = %host_key,
                    "成员粒度热重载完成（宿主进程内重载，其余成员不受影响；并集指纹已刷新）"
                );
                Ok(())
            }
            Ok(Err(e)) => Err(PluginError {
                message: format!("reload_member: 宿主应答错误（{plugin_id}）: {e}"),
                code: None,
                source: None,
            }),
            Err(_) => Err(PluginError {
                message: format!(
                    "reload_member: 宿主应答超时（{plugin_id}，{}s）",
                    RELOAD_MEMBER_TIMEOUT.as_secs()
                ),
                code: None,
                source: None,
            }),
        }
    }

    /// 成员在场保证（域事件投递前的唤醒原语，ADR 2026-10-28 D5；也是新成员
    /// 惰性装箱的补齐原语，ADR 2026-10-29）。
    ///
    /// 三路判定：
    /// - 无存活宿主（未 spawn/已驱逐）→ Ok(no-op)：投递路径自己会拉起宿主；
    /// - 独占宿主 → Ok(no-op)：独占进程无成员级摘除面，成员恒在场；
    /// - 存活组宿主 → 发 `agentos/load_member`（带应答、幂等）。宿主协议错误
    ///   （旧宿主不认识该方法）/ 超时 → Err，由调用方告警留痕——**不得**把
    ///   「唤醒失败」静默当成「已送达」（这正是 2026-10-28 事故的病根）。
    ///
    /// 应答成功即同步记账：成员并入 `spawned_members`（宿主实有集合的镜像，
    /// 权威是分配表）并撤销待重加入标记——镜像只由协议应答更新，不自行推断。
    pub async fn ensure_group_member_present(&self, plugin_id: &str) -> Result<(), PluginError> {
        let Some(host_key) = self.existing_host_key_for(plugin_id) else {
            return Ok(());
        };
        if parse_group_slot(&host_key).is_none() {
            return Ok(());
        }
        let Some(client) = self.mcp_clients.read().get(&host_key).cloned() else {
            return Ok(());
        };
        let declared = client.read().await.declared_capabilities();
        let params = json!({ "plugin_id": plugin_id, "capabilities": declared });
        let outcome = tokio::time::timeout(LOAD_MEMBER_TIMEOUT, async move {
            client
                .read()
                .await
                .request(LOAD_MEMBER_METHOD, params)
                .await
        })
        .await;
        match outcome {
            Ok(Ok(_)) => {
                // 成员已确认在场：镜像并入 + 撤销暂离标记（ADR 2026-10-29）。
                if let Some(members) = self.spawned_members.write().get_mut(&host_key) {
                    if !members.iter().any(|m| m == plugin_id) {
                        members.push(plugin_id.to_string());
                        members.sort();
                    }
                }
                {
                    let mut pending_map = self.pending_rejoin_members.write();
                    let now_empty = match pending_map.get_mut(&host_key) {
                        Some(set) => {
                            set.remove(plugin_id);
                            set.is_empty()
                        }
                        None => false,
                    };
                    if now_empty {
                        pending_map.remove(&host_key);
                    }
                }
                // 并集基线同窗刷新（与镜像并入同一逻辑事件）：唤醒/补齐路径里
                // 分配表已先行扩员（调用入口 resolve_host_key 装箱），并集缓存
                // 不刷新的话下一次 fast path 以失步基线误判 stale → 热换
                // NoChange 回退 → 整组误杀（2026-11-01 真机 P0 根因）。
                // 锁卫生：parking_lot 写守卫已出作用域——刷新内部做全成员
                // 指纹 stat 走查（proot 下百毫秒级），绝不持同步锁做同步 IO。
                self.refresh_union_fingerprint_baseline(&host_key);
                Ok(())
            }
            Ok(Err(e)) => Err(PluginError {
                message: format!("ensure_member_present: 宿主应答错误（{plugin_id}）: {e}"),
                code: None,
                source: None,
            }),
            Err(_) => Err(PluginError {
                message: format!(
                    "ensure_member_present: 宿主应答超时（{plugin_id}，{}s）",
                    LOAD_MEMBER_TIMEOUT.as_secs()
                ),
                code: None,
                source: None,
            }),
        }
    }

    /// 成员粒度卸载实现：向成员所在组宿主发 `agentos/unload_member` 请求
    /// （带应答），宿主进程内摘除该成员（定向 on_unload + 三张聚合表原位
    /// 重聚合 + 模块缓存摘除），其余成员进程不动——卸载爆炸半径从整组收缩到
    /// 单成员（ADR 2026-09-24-member-granularity-unload-mode-panel-warmup）。
    ///
    /// 三路判定（与 [`PluginInvoker::reload_member`] 契约同构）：
    /// - 独占宿主 / 组内最后成员 → Err（调用方回退整组路径：独占单进程杀即
    ///   等价粒度；最后成员卸载 = 进程级回收，空成员集破坏合宿不变量）；
    /// - 无存活进程（未装箱/未 spawn/已驱逐）→ Ok(no-op)：无进程内服务面，
    ///   记账由调用方按需清理；
    /// - 存活组宿主 → 应答成功 Ok 后完成内核侧记账收尾（见
    ///   [`Self::finalize_member_unload`]）；宿主协议错误/超时 Err——宿主状态
    ///   未被破坏（成员仍在服务），回退整组驱逐路径安全。
    pub async fn unload_group_member(&self, plugin_id: &str) -> Result<(), PluginError> {
        let Some(host_key) = self.existing_host_key_for(plugin_id) else {
            return Ok(());
        };
        if parse_group_slot(&host_key).is_none() {
            return Err(PluginError {
                message: format!("unload_member: {plugin_id} 不在合宿组宿主（独占无进程内卸载面）"),
                code: None,
                source: None,
            });
        }
        // 最后成员判定口径 = **spawn 快照**（进程实际服务面）：软卸载保留分配
        // 条目（待重加入标记，见 finalize_member_unload），按分配表长判定会被
        // 待重加入成员虚胖——卸掉进程内最后一名服务成员会得到空成员集进程，
        // 破坏合宿不变量（host.py 侧同样拒绝，双保险同口径）。
        let process_members = self
            .spawned_members
            .read()
            .get(&host_key)
            .cloned()
            .unwrap_or_else(|| self.host_members(&host_key));
        if process_members.len() <= 1 {
            return Err(PluginError {
                message: format!(
                    "unload_member: {plugin_id} 是宿主 {host_key} 的最后成员（须走整组回收）"
                ),
                code: None,
                source: None,
            });
        }
        let Some(client) = self.mcp_clients.read().get(&host_key).cloned() else {
            return Ok(());
        };
        let params = json!({ "plugin_id": plugin_id });
        let outcome = tokio::time::timeout(RELOAD_MEMBER_TIMEOUT, async move {
            client
                .read()
                .await
                .request(UNLOAD_MEMBER_METHOD, params)
                .await
        })
        .await;
        match outcome {
            Ok(Ok(result)) => {
                if result.get("unloaded").and_then(Value::as_bool) != Some(true) {
                    return Err(PluginError {
                        message: format!(
                            "unload_member: 宿主应答缺少 unloaded=true（{plugin_id}）"
                        ),
                        code: None,
                        source: None,
                    });
                }
                self.finalize_member_unload(&host_key, plugin_id).await;
                info!(
                    plugin_id = plugin_id,
                    host = %host_key,
                    "成员粒度卸载完成（宿主进程内摘除，其余成员不受影响；记账与并集指纹已刷新）"
                );
                Ok(())
            }
            Ok(Err(e)) => Err(PluginError {
                message: format!("unload_member: 宿主应答错误（{plugin_id}）: {e}"),
                code: None,
                source: None,
            }),
            Err(_) => Err(PluginError {
                message: format!(
                    "unload_member: 宿主应答超时（{plugin_id}，{}s）",
                    RELOAD_MEMBER_TIMEOUT.as_secs()
                ),
                code: None,
                source: None,
            }),
        }
    }

    /// 成员卸载成功后的内核侧记账收尾（请求应答成功后调用，恰一次）。
    ///
    /// - OnUnload 旁路广播 + loader 卸载（能力/路由登记面摘除）；
    /// - `spawned_members` 快照摘除该成员（进程服务面已收缩）并记入待重加入集；
    ///   **分配表条目保留**——期望成员集不变（暂离 ≠ 成员集变化），漂移检测对
    ///   暂离成员免疫，其余成员的调用照常复用宿主进程；被卸成员**自己**被调用
    ///   时触发整宿主 respawn，按分配表重建成员集，把全部软卸载成员**一次性**
    ///   带回进程。若同摘分配条目，每个被卸成员都要各自重装箱 → 各自触发一次
    ///   整宿主 respawn，k 次整杀无法收敛为 1 次（f3a6a5e39）；
    /// - 清成员空闲时钟与成员 in-flight 条目；刷新宿主并集指纹（分配表未变，
    ///   刷新后与旧值一致——指纹是 spawn 期望态，不因软卸载漂移）。
    pub(super) async fn finalize_member_unload(&self, host_key: &str, plugin_id: &str) {
        self.emit_lifecycle_unload(plugin_id);
        if let Err(e) = self.loader.unload(plugin_id).await {
            warn!(
                "loader unload {} (member unload from {}) failed: {}",
                plugin_id, host_key, e
            );
        }
        if let Some(members) = self.spawned_members.write().get_mut(host_key) {
            members.retain(|m| m != plugin_id);
        }
        self.pending_rejoin_members
            .write()
            .entry(host_key.to_string())
            .or_default()
            .insert(plugin_id.to_string());
        self.member_last_used.write().remove(plugin_id);
        self.member_inflight_calls.write().remove(plugin_id);
        let fp = self.host_union_fingerprint(host_key);
        self.fingerprints
            .write()
            .insert(host_key.to_string(), (fp, Instant::now()));
    }

    /// 按插件 id 卸载其宿主（idle GC 与 force_unload 的公共实现）。
    ///
    /// `reclaim_assignments`：idle 语境（GC 回收）为 true——连同清掉分配表内
    /// 该宿主的全部成员条目，槽位释放供后续装箱复用（§4.8「回收即清空」）；
    /// 热重载/崩溃恢复语境为 false——保留分配表，respawn 按表重建成员集。
    async fn unload_plugin_host(
        &self,
        plugin_id: &str,
        reclaim_assignments: bool,
    ) -> Result<(), PluginError> {
        match self.existing_host_key_for(plugin_id) {
            Some(host_key) => self.unload_host(&host_key, reclaim_assignments).await,
            None => {
                // 无宿主（light 从未分配 / InProcess / manifest 已不可得且无分配）：
                // 无进程可杀，仅做 loader 侧卸载 + OnUnload 旁路广播（与无缓存路径
                // 的既有语义对齐）。
                self.emit_lifecycle_unload(plugin_id);
                if let Err(e) = self.loader.unload(plugin_id).await {
                    warn!("loader unload {} (no-host path) failed: {}", plugin_id, e);
                }
                Ok(())
            }
        }
    }

    /// 卸载宿主：kill 进程 + 清缓存/指纹/last_used（记账粒度全部按宿主键）。
    ///
    /// 宿主是进程所有权单位（方案 §〇）：合宿宿主的 kill 连坐全部成员——
    /// OnUnload 旁路广播与 loader.unload 逐成员执行；分配表条目按
    /// `reclaim_assignments` 决定是否释放（见 [`Self::unload_plugin_host`]）。
    /// kill 段（取写锁 → on_unload → kill）限时 2s：在飞调用持读锁时放弃
    /// kill 照常完成驱逐与记账（残留进程由 kill_on_drop 兜底），GC 单循环
    /// 不因单个宿主阻塞。
    pub(super) async fn unload_host(
        &self,
        host_key: &str,
        reclaim_assignments: bool,
    ) -> Result<(), PluginError> {
        let members = self.host_members(host_key);

        // 旁路广播 OnUnload（杀进程/卸载之前，逐成员）。与 get_or_create_mcp_client
        // 里的 OnLoad emit 对称：观察层（审计日志 / `lifecycle.plugin_*` 指标）关注
        // "插件进程即将被卸载"这一事实。best-effort、非阻塞；未注入总线（`None`，
        // 如单测）时 no-op。
        for member in &members {
            self.emit_lifecycle_unload(member);
        }

        let client_arc = {
            let mut clients = self.mcp_clients.write();
            clients.remove(host_key)
        };

        if let Some(client_arc) = client_arc {
            // 卸载尾段（取写锁 → on_unload 通知 → kill）限时 2s：长流式调用
            // 持读锁时，无超时写锁会让 GC 单循环在此等数小时（全局空闲回收
            // 瘫痪）。超时放弃本次 kill——缓存条目已驱逐、记账清理由下方照常
            // 完成（下次调用 respawn 新实例），残留进程由 kill_on_drop 在最后
            // 一个 Arc 释放时兜底终止。先例：shutdown_all / kill_sidecar_if_any
            // 的逐 kill 2s 超时。
            let teardown = async {
                let mut client = client_arc.write().await;
                // 镜像 OnLoad 的 notifications/on_load：杀进程之前发 on_unload 给宿主一个
                // 收尾机会（fire-and-forget，不等响应）。失败仅 warn 不阻断——进程可能已崩溃
                // 或不响应该通知，该杀仍杀（卸载语义不变）。
                let _ = client
                    .send_notification("notifications/on_unload", None)
                    .await
                    .inspect_err(|e| {
                        warn!("on_unload notification failed for {}: {}", host_key, e)
                    });
                if let Err(e) = client.kill().await {
                    warn!("Failed to kill host {}: {}", host_key, e);
                }
            };
            if tokio::time::timeout(Duration::from_secs(2), teardown)
                .await
                .is_err()
            {
                warn!(
                    "unload_host: teardown of {} timed out (2s, write lock held by in-flight caller); \
                     host evicted, residual process left to kill_on_drop backstop",
                    host_key
                );
            }
        }

        // Job 守卫随记录释放：置于 teardown 之后——on_unload 通知与显式 kill
        // 的优雅收尾窗口不被 Job 关闭抢占；teardown 超时（deferred）情形由
        // kill-on-close 兜底终结残留树（取舍见 host_job 模块注释第 2 条）。
        self.release_host_job(host_key);

        // 也通过 loader 卸载（宿主粒度：合宿组连坐全部成员）
        for member in &members {
            if let Err(e) = self.loader.unload(member).await {
                warn!(
                    "loader unload {} (host {} teardown) failed: {}",
                    member, host_key, e
                );
            }
        }

        // 清除指纹缓存 + 成员记账（空闲时钟/in-flight 按成员键），下次调用重新计算并 respawn
        self.fingerprints.write().remove(host_key);
        // spawn 每成员指纹快照与成员集快照同生命周期清理（D2）
        self.host_member_fingerprints.write().remove(host_key);
        // 显式卸载/驱逐清 spawn 退避账（操作者介入通道：重启用/卸载即重开熔断预算）
        self.spawn_backoff.reset(host_key);
        {
            let mut last_used = self.member_last_used.write();
            let mut member_inflight = self.member_inflight_calls.write();
            for member in &members {
                last_used.remove(member);
                member_inflight.remove(member);
            }
        }
        // 清除 spawn 成员集快照（与 mcp_clients 条目同生命周期，见字段注释）
        self.spawned_members.write().remove(host_key);
        self.pending_rejoin_members.write().remove(host_key);
        // 清除 in-flight 计数条目（与 mcp_clients 同生命周期；在飞 guard 持有
        // Arc 克隆，减数不依赖条目存活，见 inflight_calls 字段注释）
        self.inflight_calls.write().remove(host_key);
        // 创建时刻记账同生命周期清理（guard 持 Arc 克隆自清 token，不依赖条目）
        self.inflight_since.write().remove(host_key);
        // B8 轮换记账随记录清理（respawn 归零重计，见 host_request_counts 字段注释）
        self.host_request_counts.write().remove(host_key);

        // idle GC 回收语境：清分配表内该宿主的全部成员条目——槽位全部释放，
        // 后续新插件装箱时优先复用（§4.5 第 3 条 / §4.8「回收即清空」）。
        if reclaim_assignments {
            self.light_packing
                .write()
                .assignments
                .retain(|_, hk| hk.as_str() != host_key);
        }

        info!(
            "Force unloaded host: {} (members: {:?}, reclaim_slots: {})",
            host_key, members, reclaim_assignments
        );
        Ok(())
    }

    /// 旁路广播单成员 OnUnload（unload_host 逐成员调用）。
    fn emit_lifecycle_unload(&self, plugin_id: &str) {
        let bus_guard = self.hook_bus.read();
        let Some(bus) = bus_guard.as_ref() else {
            return;
        };
        let mut ctx = HookContext::new();
        ctx.set("plugin_id", json!(plugin_id));
        bus.emit(LifecycleEvent {
            hook: LifecycleHook::OnUnload,
            ctx,
            target: EventTarget::Plugin(plugin_id.to_string()),
            ts: SystemTime::now(),
        });
    }

    /// Pull 热加载核心：判断缓存的 sidecar 进程是否因代码/配置变更而过期。
    ///
    /// 返回 `true` 表示插件已更新、需要 kill 旧进程 respawn 加载新版本；
    /// 返回 `false` 表示可直接复用缓存进程。
    ///
    /// 策略（双层短路，热路径零开销）：
    /// 1. **TTL 门**：距上次检测不足 `PLUGIN_FINGERPRINT_TTL`（1s）→ 直接返回 false。
    ///    高频调用同一插件时避免反复 stat 文件系统。
    /// 2. **指纹比对**：TTL 过期后 stat 插件目录 + config_files 的 mtime，与缓存指纹比对。
    ///    相同 → 刷新时刻戳返回 false（没更新）；不同 → 更新缓存返回 true（已更新）。
    ///
    /// 首次调用（缓存无记录）→ 计算指纹写入缓存返回 false（首次走 spawn，不是热更新）。
    pub(super) async fn is_plugin_stale(&self, plugin_id: &str, manifest: &PluginManifest) -> bool {
        let now = Instant::now();
        // TTL 门 + 指纹比对在同一把读锁下原子完成（计算指纹前先快照缓存）。
        let cached = self.fingerprints.read().get(plugin_id).cloned();
        match cached {
            None => {
                // 首次：写入指纹，不算过期（此时进程刚 spawn，必是最新）
                let fp = self.resolve_fingerprint(manifest);
                self.fingerprints
                    .write()
                    .insert(plugin_id.to_string(), (fp, now));
                false
            }
            Some((old_fp, last_check)) => {
                // TTL 门：未到期直接复用
                if now.duration_since(last_check) < PLUGIN_FINGERPRINT_TTL {
                    return false;
                }
                // TTL 过期：stat 当前指纹比对
                let new_fp = self.resolve_fingerprint(manifest);
                let stale = new_fp != old_fp;
                // 无论是否过期都刷新检测时刻；指纹变了才更新缓存指纹
                let to_store = if stale { new_fp } else { old_fp };
                self.fingerprints
                    .write()
                    .insert(plugin_id.to_string(), (to_store, now));
                stale
            }
        }
    }

    /// 解析插件指纹：复用 loader 的 get_plugin_dir 拿目录，再调 compute_plugin_fingerprint。
    /// 拿不到目录（插件已被移除）→ 返回 0，调用方会把这当作"指纹变化"触发 respawn，
    /// respawn 时会正常报错（插件已不存在），行为合理。
    pub(super) fn resolve_fingerprint(&self, manifest: &PluginManifest) -> u64 {
        match self.loader.get_plugin_dir(&manifest.id) {
            Some(dir) => compute_plugin_fingerprint(std::path::Path::new(&dir), manifest),
            None => 0,
        }
    }
}
