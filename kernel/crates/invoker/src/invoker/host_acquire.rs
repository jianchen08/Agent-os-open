//! 宿主获取链：宿主键路由与装箱、per-host spawn 锁 single-flight、复用判定
//! （fast path / double-check）、传输三路分流构造、connect+initialize 握手与
//! 缓存登记。
//!
//! 自 invoker.rs 按关注点纯搬运拆出（2026-10-12 文件长度机械闸首个应用案例，
//! ADR docs/decisions/2026-10-12-file-length-mechanical-gate.md）。

use std::time::SystemTime;

use agentos_hooks::{EventTarget, LifecycleEvent};
use agentos_mcp::resolve_env_placeholders;
use futures_util::stream::{self, StreamExt};
use serde_json::json;
use tracing::error;

use super::host_key::{
    cohost_packing_domain, diff_member_fingerprints, group_host_key, host_start_timeout,
    is_cohost_member, light_host_max_members, parse_group_slot, scoped_router_identity,
    solo_host_key, union_fingerprint_of, MemberFingerprintDiff, PluginScopedRouter,
    DEFAULT_HOST_GROUP, GROUP_HOST_DIR, PLUGIN_FINGERPRINT_TTL,
};
use std::path::Path;

use super::inflight_gc::StartingPluginsGuard;
use super::venv::{
    find_user_space_venv_interpreter, find_venv_interpreter, is_plain_python_command,
    resolve_venv_direct_interpreter_for_spawn,
};

use super::*;

impl PluginInvokerImpl {
    /// 获取或创建 MCP 客户端（按需加载，宿主键粒度——合宿进程模型 §4.2）。
    ///
    /// light 合宿插件首次调用时经 [`Self::resolve_host_key`] 动态装箱到
    /// `group:light:{n}` 宿主；其余 sidecar 走 `plugin:{plugin_id}` 独占宿主。
    /// 并发安全：用 per-host spawn 锁（single-flight）保证同一宿主键的 spawn
    /// 串行化——并发触发只创建一个宿主进程，不遗留孤儿进程；跨宿主并行。
    pub(super) async fn get_or_create_mcp_client(
        &self,
        manifest: &PluginManifest,
    ) -> Result<Arc<tokio::sync::RwLock<McpClient>>, PluginError> {
        // 宿主键解析：light 首次调用装箱分配（粘性），此后命中分配表直读。
        let host_key = self.resolve_host_key(manifest);

        // Fast path：无锁查缓存，命中且未死直接复用（热路径，避开 spawn 锁开销）；
        // 已死/宿主指纹过期就地 kill+驱逐，落到 slow path respawn。判死与复活
        // 门控细节见 reuse_cached_host_fast_path。
        if let Some(client) = self.reuse_cached_host_fast_path(&host_key, manifest).await {
            return Ok(client);
        }

        // Slow path：拿 per-host spawn 锁，串行化同宿主键的 spawn（single-flight
        // ——并发触发只创建一个宿主进程，不遗留孤儿进程；跨宿主并行）。
        // 取（或创建）该宿主的专用锁 Arc——锁本身常驻 spawn_locks，不随调用释放。
        let spawn_lock = {
            let mut locks = self.spawn_locks.write();
            locks
                .entry(host_key.clone())
                .or_insert_with(|| Arc::new(tokio::sync::Mutex::new(())))
                .clone()
        };
        let _spawn_guard = spawn_lock.lock().await;

        // Double-check：持 spawn 锁后再查缓存——前一个持锁者可能已创建好 client，
        // 命中即复用避免重复 spawn（判死门控与 fast path 同款；极端情况 double-check
        // 时又崩溃 → kill 后继续走下方 spawn）。细节见 reuse_fresh_host_locked。
        if let Some(client) = self.reuse_fresh_host_locked(&host_key, &manifest.id).await {
            return Ok(client);
        }

        // D1 宽限面：spawn 窗口记账（本次加载的全体成员，组宿主 = 成员集快照
        // 基准即当前分配表，独占 = 单插件）。guard 持写锁摘除，成功/失败/panic
        // 出窗必清——HTTP dispatcher 在窗口内暂停端点超时预算（等待就绪而非
        // 504），泄漏一个条目 = 该插件端点宽限常开，必须出窗即清。
        let starting_members = match parse_group_slot(&host_key) {
            Some(_) => self.host_members(&host_key),
            None => vec![manifest.id.clone()],
        };
        {
            let mut starting = self.starting_plugins.write();
            for member in &starting_members {
                starting.insert(member.clone());
            }
        }
        let _starting_guard = StartingPluginsGuard {
            set: &self.starting_plugins,
            members: starting_members,
        };

        // 卡2 熔断闸：连续 spawn 失败先指数退避、达阈值即熔断停自动 spawn——
        // 终结对坏插件的无限 respawn（装机 2026-10-17 实证 535 次 ImportError
        // 循环）。指纹变化（用户动手修）自动清账放行：熔断的是对未修复插件的
        // 自动重试，不是修复本身。
        let current_fp = self.resolve_fingerprint(manifest);
        match self.spawn_backoff.gate(&host_key, current_fp) {
            GateDecision::Allow => {}
            GateDecision::Backoff { retry_after } => {
                return Err(PluginError {
                    message: format!(
                        "宿主 {host_key} spawn 退避中（{:.1}s 后可重试）；末次错误: {}",
                        retry_after.as_secs_f32(),
                        self.spawn_backoff.last_error(&host_key).unwrap_or_default()
                    ),
                    code: Some("HOST_SPAWN_BACKOFF".to_string()),
                    source: Some("plugin-invoker".to_string()),
                });
            }
            GateDecision::BreakerOpen => {
                return Err(PluginError {
                    message: format!(
                        "宿主 {host_key} 连续 spawn 失败已熔断，停止自动 spawn（插件判定当前不可用）；\
                         修复插件文件后自动恢复，或重启用插件/卸载宿主重开预算。末次错误: {}",
                        self.spawn_backoff.last_error(&host_key).unwrap_or_default()
                    ),
                    code: Some("HOST_SPAWN_BREAKER_OPEN".to_string()),
                    source: Some("plugin-invoker".to_string()),
                });
            }
        }

        // D5 预算闸准入（ADR 2026-10-19 第一刀）：预算启用且采样闭包判定超限
        // → 拒绝 spawn（结构化错误信封 + WARN，metric 计数在装配方闭包内）。
        // 拒绝是系统级负载 shedding 不是插件失败：**不入** spawn_backoff 记账
        // （不触发熔断、不发插件不可用通知）——预算回落后下一次调用自然放行。
        // 回调缺席（预算未启用）= 直通零开销；检查点在 double-check 之后，命中
        // 缓存复用（零 spawn）的调用不经本闸。
        if let Some(gate) = self.pre_spawn_gate() {
            if let Err(reason) = gate() {
                // 消息与 reason 字段同行承载：llvm-cov 对多行宏独占一行的
                // 消息字面量单列 region 恒 0 命中（度量假象，误触 diff
                // coverage 门禁），同行即并入命中行（同文件 180 行存量同形）。
                tracing::warn!(
                    target: "invoker-spawn",
                    host = %host_key,
                    plugin = %manifest.id,
                    reason = %reason, "宿主 spawn 被内存预算闸拒绝（load shedding，不入退避记账）"
                );
                return Err(PluginError {
                    message: format!("宿主 {host_key} spawn 被内存预算闸拒绝：{reason}"),
                    code: Some("HOST_SPAWN_BUDGET_REJECTED".to_string()),
                    source: Some("plugin-invoker".to_string()),
                });
            }
        }

        // 构造新客户端（持有 spawn 锁保证串行）：传输三路分流由 manifest.mcp
        // 决定（HTTP 远程 / 外部 stdio 命令 / 项目自带 sidecar），见 build_mcp_client。
        // 成败都记账：成功清账；失败连击（跨线熔断 warn 一次，通知面见卡3）。
        let outcome = async {
            let client = self.build_mcp_client(&host_key, manifest)?;
            self.connect_initialize_and_cache(client, &host_key, manifest)
                .await
        }
        .await;
        match &outcome {
            Ok(_) => {
                // 卡3 恢复沿：熔断宿主被一次成功 spawn 收束 → 恢复通知判定事件
                // （record_success 返回是否收束了已熔断宿主，恰好一次；内核据此
                // 发 kind=restored 系统通知。未熔断的普通成功 false = 无通知）。
                if self.spawn_backoff.record_success(&host_key) {
                    let members = parse_group_slot(&host_key)
                        .map(|_| self.host_members(&host_key))
                        .unwrap_or_else(|| vec![manifest.id.clone()]);
                    self.notify_spawn_recovered(&SpawnRecoveredNotice {
                        host_key: host_key.to_string(),
                        plugin_id: manifest.id.clone(),
                        members,
                    });
                }
            }
            Err(e) => {
                let tripped = self
                    .spawn_backoff
                    .record_failure(&host_key, current_fp, &e.message);
                if tripped {
                    warn!(
                        target: "invoker-spawn",
                        plugin = %manifest.id,
                        host = %host_key,
                        error = %e.message,
                        "spawn 连续失败达熔断阈值：停止自动 spawn（插件判定当前不可用；修复插件文件自动恢复）"
                    );
                    // 卡2③/卡3：判定事件外送（内核据此发系统通知；未注入回调
                    // 时仅上述 warn，行为不变）。
                    let members = parse_group_slot(&host_key)
                        .map(|_| self.host_members(&host_key))
                        .unwrap_or_else(|| vec![manifest.id.clone()]);
                    self.notify_spawn_breaker(&SpawnBreakerNotice {
                        host_key: host_key.to_string(),
                        plugin_id: manifest.id.clone(),
                        members,
                        consecutive: self.spawn_backoff.consecutive(&host_key),
                        last_error: e.message.clone(),
                    });
                }
            }
        }
        outcome
    }

    /// 请求路径整组并行 respawn（Phase 3 #1，trait [`PluginInvoker::ensure_hosts_ready`]
    /// 的实现体）：管道链撞死宿主后一次并行拉起全链 + 就绪等待。
    ///
    /// - 就绪等待 = 逐宿主走 [`Self::get_or_create_mcp_client`] 全程（spawn→
    ///   连接→initialize→on_load），await 收齐即全链可服务；存活宿主 fast path
    ///   命中缓存 no-op，零 spawn。
    /// - 并行度有界 `min(8, 逻辑核数)`（[`agentos_core::types::bounded_spawn_concurrency`]）；
    ///   per-host single-flight 锁保证同宿主并发触发不双 spawn，跨宿主真并行。
    /// - 装箱预分配先行（与 boot 预热同规）：合宿 `--members`/组指纹取分配表
    ///   快照，先分配完全体再并发拉起，每宿主一次 spawn 装载完整成员集。
    /// - 失败隔离：单宿主拉起失败只 warn（懒 spawn 运行期兜底），不拖死整组。
    /// - 刻意**不**经 `warmup_sidecar`（不登记 keep-warm 常驻集，idle GC 治理
    ///   不变——常驻声明面用户裁定挂起）。
    pub(super) async fn ensure_hosts_ready_impl(&self, plugin_ids: &[String]) {
        if plugin_ids.is_empty() {
            return;
        }
        // 声明面依赖闭包（R7 F3，单跳不传递）：引用集内插件的 requires_services
        // 点式条目（ns.method = 精确运行时调用面）解析出声明提供方并一并并行
        // 拉起——llm_core 声明 llm.complete_stream → llm_service 宿主随 run 起手
        // 就绪，litellm 导入/Router 构建离开回收后首调关键路径。bare 命名空间
        // 条目不展开（全量 packaging 样板属注册闸存在性语义，展开会把
        // python_packager 等无关提供方拖进每次 run）；boot 预热集
        // （referenced_plugin_ids 单源）不受影响——工具/服务插件保持纯懒加载
        // （keep-warm 常驻属挂起裁定面）。提供方缺失/解析空 = 静默跳过（懒
        // spawn 运行期兜底，与整组 respawn 失败容忍同口径）。
        let mut ids: Vec<String> = plugin_ids.to_vec();
        for id in plugin_ids {
            let Some(manifest) = self.loader.get_manifest(id) else {
                continue;
            };
            for item in &manifest.requires_services {
                if !item.contains('.') {
                    continue;
                }
                for provider in self.loader.service_provider_ids(item) {
                    if !ids.contains(&provider) {
                        ids.push(provider);
                    }
                }
            }
        }
        let manifests: Vec<PluginManifest> = ids
            .iter()
            .filter_map(|id| self.loader.get_manifest(id))
            .filter(|m| m.host_type == HostType::Sidecar)
            .collect();
        if manifests.is_empty() {
            return;
        }
        for m in &manifests {
            self.preassign_host_key(m);
        }
        let concurrency = agentos_core::types::bounded_spawn_concurrency();
        let total = manifests.len();
        let results: Vec<(String, Result<(), PluginError>)> = stream::iter(manifests)
            .map(|m| async move {
                let id = m.id.clone();
                (id, self.get_or_create_mcp_client(&m).await.map(|_| ()))
            })
            .buffer_unordered(concurrency)
            .collect()
            .await;
        let failed: Vec<&String> = results
            .iter()
            .filter_map(|(id, r)| r.as_ref().err().map(|_| id))
            .collect();
        if !failed.is_empty() {
            warn!(
                target: "invoker-ensure",
                total,
                failed = failed.len(),
                failed_ids = ?failed,
                "请求路径整组 respawn 有失败（懒 spawn 运行期兜底，不影响其余宿主）"
            );
        }
    }

    /// Fast path 复用判定（无锁查缓存）：命中且存活按指纹新旧二分——
    ///
    /// - **新鲜**：`touch_last_used` 后返回 Some（直接复用，零 spawn）。
    /// - **Pull 热加载触发**（TTL 过后 stat mtime 发现变化）：kill 旧宿主并驱逐，
    ///   不调 notify_crash（主动热更新非崩溃），返回 None 进 slow path respawn；
    ///   合宿宿主按当前分配表重建成员集。TTL 门 + 指纹比对语义：合宿宿主 =
    ///   成员指纹并集；未用到也不检测——纯按需 pull。
    /// - **stdio 宿主真死**（pid=Some 且进程退出；HTTP transport pid=None 恒不进
    ///   此分支）：error 留痕 + kill 驱逐；合宿宿主死亡 = 全组成员进程死，逐成员
    ///   触发崩溃回调（能力卸载语义）。返回 None 进 slow path。
    /// - **未命中**：直接 None（无副作用）。
    pub(super) async fn reuse_cached_host_fast_path(
        &self,
        host_key: &str,
        manifest: &PluginManifest,
    ) -> Option<SharedMcpClient> {
        let cached = {
            let clients = self.mcp_clients.read();
            clients.get(host_key).cloned()
        };
        let client = cached?;
        let client_guard = client.read().await;
        if Self::is_dead_sidecar(&client_guard).await {
            error!("Host process crashed: {}", host_key);
            drop(client_guard);
            for member in self.host_members(host_key) {
                self.notify_crash(&member);
            }
            self.kill_and_evict_if_current(host_key, &client).await;
            return None;
        }
        let drifted = self.light_host_members_drifted(host_key, &manifest.id);
        drop(client_guard);
        // 成员集差异优先按权威补齐（ADR 2026-10-29）：新成员惰性装箱进存活宿主
        // 是正常事件，不该付整组重建的代价。补齐失败才走整组驱逐（现状语义）。
        if drifted && !self.sync_host_member_set(host_key, &manifest.id).await {
            info!("Host member set sync failed, reloading host: {}", host_key);
            self.kill_and_evict_if_current(host_key, &client).await;
            return None;
        }
        if !self.host_code_stale(host_key, manifest).await {
            self.touch_member_last_used(&manifest.id);
            return Some(Arc::clone(&client));
        }
        // 成员集已补齐或本就一致：过期原因只剩代码/配置面变，走 D2 两段式
        // （先试成员级定向热换，失败回退整组驱逐）。
        if self.try_member_level_hot_swap(host_key).await {
            self.touch_member_last_used(&manifest.id);
            return Some(Arc::clone(&client));
        }
        info!(
            "Host code/config or member set changed, reloading host: {}",
            host_key
        );
        self.kill_and_evict_if_current(host_key, &client).await;
        None
    }

    /// Pull 路径成员级热换（ADR 2026-10-19 D2，`host_code_stale` 判过期后调用）：
    /// 以 spawn 每成员指纹表为进程内容基准逐成员定位变更，仅成员代码/配置面
    /// 变（成员集不变）时对变更成员走既有 `reload_group_member` 通道（进程内
    /// swap，含声明面聚合表原位更新与并集指纹刷新），全部成功返回 true——
    /// 宿主进程与连接保持不动。
    ///
    /// 回退整组重建（返回 false）的情形：
    /// - 独占宿主（无进程内重载面，单进程杀即等价粒度）；
    /// - spawn 每成员指纹表缺失（进程内容未知，BUG-44 同哲学 fail-closed）；
    /// - 成员集变化（增/删成员，`SetChanged`）；
    /// - 任一成员 `reload_member` 失败（未知成员/协议错误/超时）。
    ///
    /// `NoChange`（逐成员全同）不是回退情形：进程内容与磁盘一致，stale 信号
    /// 必是并集基线失步（成员集补齐/唤醒并入后基线未及刷新的竞态窗）——
    /// 刷新基线即愈，绝不当整组驱逐理由（2026-11-01 真机 P0 根因）。
    pub(super) async fn try_member_level_hot_swap(&self, host_key: &str) -> bool {
        if parse_group_slot(host_key).is_none() {
            return false;
        }
        let spawned_fps = self.host_member_fingerprints.read().get(host_key).cloned();
        let Some(spawned_fps) = spawned_fps else {
            warn!(
                target: "invoker-hotswap",
                host = %host_key,
                "spawn 每成员指纹表缺失（进程内容未知），回退整组重建"
            );
            return false;
        };
        let current_fps = self.current_member_fingerprints(host_key);
        let changed = match diff_member_fingerprints(&spawned_fps, &current_fps) {
            MemberFingerprintDiff::Changed(members) => members,
            MemberFingerprintDiff::SetChanged => return false,
            MemberFingerprintDiff::NoChange => {
                // 逐成员全同 = 进程内容与磁盘一致：stale 信号必是并集基线失步
                // （成员集补齐/唤醒并入后基线未及刷新的竞态窗），不是真变更
                // ——刷新基线即愈，绝不当整组驱逐理由（2026-11-01 真机 P0）。
                self.refresh_union_fingerprint_baseline(host_key);
                return true;
            }
        };
        for pid in &changed {
            if let Err(e) = self.reload_group_member(pid).await {
                warn!(
                    target: "invoker-hotswap",
                    host = %host_key,
                    plugin_id = %pid,
                    error = %e.message,
                    "成员级热换失败，回退整组驱逐重建"
                );
                return false;
            }
        }
        info!(
            target: "invoker-hotswap",
            host = %host_key,
            reloaded = ?changed,
            "pull 路径成员级热换完成（进程内定向重载，宿主进程与其余成员不动）"
        );
        true
    }

    /// 驱逐已判定死亡/过期的宿主实例（kill + 缓存/快照逐出，按条目身份条件执行）。
    ///
    /// BUG-44 根因修复：kill 会因在飞调用持客户端读锁而阻塞数秒，苏醒后
    /// mcp_clients 条目可能已被并发 respawn 替换（新实例入缓存 + 新 spawn
    /// 成员集快照已写入）。此处若无条件 remove，会把并发 respawn 写入的
    /// 新鲜快照连带删掉，留下「缓存存活但快照缺失」的宿主——成员集漂移
    /// 检测（快照比对）失去基准，新装箱成员首调用复用旧成员集进程报
    /// MCP [-32602] tool not found（2026-09-18 00:21 实测序列）。
    /// 因此逐出前按 Arc 身份复核：条目已非本次判定的实例（被并发 respawn
    /// 替换/移除）则不动缓存与快照，只杀死本次判定的旧实例。
    ///
    /// BUG-81 根因修复：条目摘除**先于** kill，且 kill 绝不 inline 等写锁——
    /// 长等待调用（审批 wait_for_choice 24h）持读锁期间，`write().await` 的
    /// 公平排队会把全部后续读者（fast path 的 `client.read()`）队头阻塞在
    /// 驱逐者身后，整宿主所有端点连锁超时（装机版 2026-09-23T19:21 实测
    /// approval_service 504 风暴 5400+ 条）。对齐 [`Self::unload_host`] 的
    /// 既有模式：身份命中即摘条目与快照（新调用立即走 respawn 新实例），
    /// `try_write` 拿不到写锁（在飞调用未排空）就放弃本次 kill——在飞 Arc
    /// 全部释放时由 kill_on_drop 兜底终止残留进程。
    pub(super) async fn kill_and_evict_if_current(&self, host_key: &str, client: &SharedMcpClient) {
        let evicted = {
            let mut clients = self.mcp_clients.write();
            let is_current = clients
                .get(host_key)
                .is_some_and(|cur| Arc::ptr_eq(cur, client));
            if is_current {
                clients.remove(host_key);
            }
            is_current
        };
        if evicted {
            self.spawned_members.write().remove(host_key);
            self.pending_rejoin_members.write().remove(host_key);
            // spawn 每成员指纹快照与成员集快照同生命周期（D2）
            self.host_member_fingerprints.write().remove(host_key);
            // Job 守卫随记录释放（仅身份命中时——条目已被并发 respawn 替换则
            // 不动新实例的守卫）。kill-on-close 可能先于下方 try_write kill
            // 生效：被驱逐宿主已判死，「判死即终」取舍见 host_job 模块注释。
            self.release_host_job(host_key);
        }
        match client.try_write() {
            Ok(mut guard) => {
                if let Err(e) = guard.kill().await {
                    tracing::debug!(
                        "evict: best-effort kill of host {} failed (will respawn): {e}",
                        host_key
                    );
                }
            }
            Err(_) => {
                tracing::debug!(
                    "evict: kill of host {} deferred (in-flight calls hold read lock; kill_on_drop reaps)",
                    host_key
                );
            }
        }
    }

    /// Double-check 复用判定（持 spawn 锁后调用）：前一个持锁者可能已创建好
    /// client。命中且存活 → touch 后返回 Some；又崩溃 → kill+驱逐返回 None 继续
    /// spawn。判死与 fast path 同用 is_dead_sidecar 门控（HTTP transport 不判死，
    /// 防误报）；不做指纹 staleness 检测（刚 spawn/校验过的进程无需 stat），
    /// 但做成员集漂移检测——spawn 窗口内新成员装箱进本宿主（分配表已更新、
    /// 快照未写入）时，前一个持锁者 spawn 的进程成员集已过期，必须 kill 重来。
    ///
    /// BUG-81 同源修复（与 [`Self::kill_and_evict_if_current`] 同理）：条目/快照
    /// 摘除先于 kill，kill 用 `try_write` 不 inline 等写锁——在飞长调用持读锁时
    /// inline 等待会在持 spawn 锁状态下悬挂，该宿主的全部 respawn/复用连锁排队。
    /// 写锁拿不到就放弃本次 kill，残留进程由 kill_on_drop 兜底。
    pub(super) async fn reuse_fresh_host_locked(
        &self,
        host_key: &str,
        plugin_id: &str,
    ) -> Option<SharedMcpClient> {
        let cached = {
            let clients = self.mcp_clients.read();
            clients.get(host_key).cloned()
        };
        let client = cached?;
        let client_guard = client.read().await;
        let dead = Self::is_dead_sidecar(&client_guard).await;
        let drifted = !dead && self.light_host_members_drifted(host_key, plugin_id);
        drop(client_guard);
        if !dead {
            // 成员集差异先按权威补齐（ADR 2026-10-29）；补齐失败才整组重建。
            if !drifted || self.sync_host_member_set(host_key, plugin_id).await {
                self.touch_member_last_used(plugin_id);
                return Some(Arc::clone(&client));
            }
            info!(
                "Host member set sync failed (double-check), reloading host: {}",
                host_key
            );
        }
        self.mcp_clients.write().remove(host_key);
        self.spawned_members.write().remove(host_key);
        self.pending_rejoin_members.write().remove(host_key);
        // spawn 每成员指纹快照同生命周期清理（D2）
        self.host_member_fingerprints.write().remove(host_key);
        // Job 守卫随记录释放（kill-on-close 兜底整树，见 host_jobs 字段注释）
        self.release_host_job(host_key);
        match client.try_write() {
            Ok(mut guard) => {
                if let Err(e) = guard.kill().await {
                    tracing::debug!(
                        "double-check: best-effort kill of host {} failed (will respawn): {e}",
                        host_key
                    );
                }
            }
            Err(_) => {
                tracing::debug!(
                    "double-check: kill of host {} deferred (in-flight calls hold read lock; kill_on_drop reaps)",
                    host_key
                );
            }
        }
        None
    }

    /// 构造新 MCP 客户端（未连接）：传输三路分流（由 manifest.mcp 决定）——
    ///
    /// ① mcp.transport=StreamableHttp + endpoint.url → 外部远程 MCP，走 HTTP
    ///    （不 spawn），见 [`Self::build_http_transport_client`]；
    /// ② mcp.transport=Stdio + endpoint.command → 外部本地第三方命令 MCP
    ///    （如 npx），spawn endpoint.command（不经 parse_entry，entry 仅作语义
    ///    标记），见 [`Self::build_external_stdio_transport_client`]；
    /// ③ 其余（无 mcp 配置的项目自带 sidecar）→ parse_entry(entry) → stdio，
    ///    见 [`Self::build_sidecar_transport_client`]。
    ///
    /// 尾部统一应用插件级调用超时（M2 单声明派生，[`PluginManifest::
    /// declared_call_need_secs`]：`capabilities.call_timeout_seconds` ∪
    /// `mcp.request_timeout_secs` 取最大）：长等待业务（交互插件
    /// wait_for_choice 的 24h 审批等）以声明值为准；未声明时落内核 mcp 客户端
    /// 默认（86400s，BUG-60 审批族统一值）——不再有 300s 默认先于用户操作掐断
    /// 调用（-32001 超时 → 审批作废 → 引擎重试弹窗循环）。声明需求在场即同时
    /// 派生传输界：单次 MCP 往返的响应在整次调用结束才返回，传输界必须盖住
    /// 声明需求（llm_core 900<1800 倒挂的解析面修复点，ADR
    /// 2026-11-05-timeout-taxonomy-and-merge）。
    pub(super) fn build_mcp_client(
        &self,
        host_key: &str,
        manifest: &PluginManifest,
    ) -> Result<McpClient, PluginError> {
        let mut client = match manifest.mcp.as_ref() {
            Some(cfg) if cfg.transport == agentos_core::traits::McpTransport::StreamableHttp => {
                Self::build_http_transport_client(cfg, manifest)?
            }
            Some(cfg)
                if cfg.transport == agentos_core::traits::McpTransport::Stdio
                    && cfg
                        .endpoint
                        .as_ref()
                        .and_then(|e| e.command.as_ref())
                        .is_some() =>
            {
                Self::build_external_stdio_transport_client(cfg, manifest)?
            }
            _ => self.build_sidecar_transport_client(host_key, manifest)?,
        };

        if let Some(secs) = manifest.declared_call_need_secs() {
            client = client.with_request_timeout(std::time::Duration::from_secs(secs));
        }
        Ok(client)
    }

    /// 分流①：外部远程 HTTP MCP 客户端。endpoint.url 缺失即协议级配置错误
    /// （fail-closed，MCP_CONFIG_INVALID），不降级不回退。
    fn build_http_transport_client(
        cfg: &agentos_core::traits::McpConfig,
        manifest: &PluginManifest,
    ) -> Result<McpClient, PluginError> {
        let ep = cfg.endpoint.as_ref().ok_or_else(|| PluginError {
            message: format!(
                "插件 {} 声明 streamable_http 但缺 endpoint.url",
                manifest.id
            ),
            code: Some("MCP_CONFIG_INVALID".to_string()),
            source: Some("plugin-invoker".to_string()),
        })?;
        let url = ep.url.clone().ok_or_else(|| PluginError {
            message: format!("插件 {} 的 streamable_http endpoint 缺 url", manifest.id),
            code: Some("MCP_CONFIG_INVALID".to_string()),
            source: Some("plugin-invoker".to_string()),
        })?;
        tracing::info!(
            "[invoker] 插件 {} 走 HTTP transport（外部 MCP）| url={}",
            manifest.id,
            url
        );
        Ok(
            McpClient::new_http(url, ep.headers.clone(), ep.auth.clone())
                .with_plugin_id(&manifest.id),
        )
    }

    /// 分流②：外部本地第三方命令 MCP（如 npx playwright）——spawn
    /// endpoint.command。env 值含 ${ENV_VAR} 占位 → 解析（缺失则启动失败早暴露）。
    fn build_external_stdio_transport_client(
        cfg: &agentos_core::traits::McpConfig,
        manifest: &PluginManifest,
    ) -> Result<McpClient, PluginError> {
        let ep = cfg.endpoint.as_ref().expect("checked above");
        let command = match ep.command.clone() {
            Some(c) if !c.is_empty() => c,
            _ => {
                return Err(PluginError {
                    message: format!("插件 {} stdio endpoint 缺 command", manifest.id),
                    code: Some("MCP_CONFIG_INVALID".to_string()),
                    source: Some("plugin-invoker".to_string()),
                })
            }
        };
        tracing::info!(
            "[invoker] 插件 {} 走外部 stdio 命令 | command={} {}",
            manifest.id,
            command,
            ep.args.join(" ")
        );
        let mut c = McpClient::new_stdio(command, ep.args.clone()).with_plugin_id(&manifest.id);
        let mut extra_env: Vec<(String, String)> = Vec::new();
        for (k, v) in &ep.env {
            let resolved = resolve_env_placeholders(v).map_err(|e| PluginError {
                message: format!("插件 {} env 解析失败: {}", manifest.id, e),
                code: Some("MCP_CONFIG_INVALID".to_string()),
                source: Some("plugin-invoker".to_string()),
            })?;
            extra_env.push((k.clone(), resolved));
        }
        if !extra_env.is_empty() {
            c = c.with_extra_env(extra_env);
        }
        Ok(c)
    }

    /// 分流③：项目自带 sidecar 的 stdio 客户端。第四维分流（合宿进程模型 §4.2
    /// 第 2 条）：light 合宿成员 → 共享组宿主命令（host.py）；独占 → 插件自身
    /// sidecar 命令（现状语义）。两形态共用同一 spawn 锁/判死/respawn 代码路径，
    /// 仅命令与工作目录不同。
    fn build_sidecar_transport_client(
        &self,
        host_key: &str,
        manifest: &PluginManifest,
    ) -> Result<McpClient, PluginError> {
        let (command, args, working_dir) = if is_cohost_member(manifest) {
            let (group, slot) = parse_group_slot(host_key)
                .expect("合宿成员宿主键必含组名与槽位号（resolve_host_key 分配保证）");
            let members = self.host_members(host_key);
            // 记录实际 spawn 的成员集快照（漂移检测基准，见 spawned_members 字段
            // 注释）。先装箱后 spawn 的调用序保证：此处快照 = 本次 spawn 的
            // --members 注入集；快照写入先于客户端入缓存，fast path 判定时必已
            // 就位。失败路径（resolve_group_host_command 报错）不写快照——进程
            // 未 spawn，无漂移可言。
            self.spawned_members
                .write()
                .insert(host_key.to_string(), members.clone());
            // D2：spawn 每成员指纹快照同点写入（pull 路径成员级定位基准，
            // 见 host_member_fingerprints 字段注释）；与成员集快照同生命周期。
            self.snapshot_member_fingerprints(host_key);
            // respawn 按分配表重建成员集：全部暂离成员随本次 spawn 回归进程，
            // 待重加入记账清空（与快照同点刷新，见 pending_rejoin_members 注释）
            self.pending_rejoin_members.write().remove(host_key);
            let (command, args, workdir) = self.resolve_group_host_command(
                group,
                slot,
                &members,
                builtin_group_host_dir().as_deref(),
            )?;
            (command, args, Some(workdir))
        } else {
            let (command, args) = self.resolve_sidecar_command(manifest)?;
            let workdir = self.loader.get_plugin_dir(&manifest.id);
            (command, args, workdir)
        };
        // Windows venv trampoline 消层：独占/合宿统一收口——命令直达 base
        // 解释器 + __PYVENV_LAUNCHER__ 锚回 venv 身份（非 venv python 形态
        // 恒原样，见 [`resolve_venv_direct_interpreter`]）。
        let (command, args, venv_direct_env) =
            resolve_venv_direct_interpreter_for_spawn(&command, args);
        let mut c = McpClient::new_stdio(command, args)
            // 宿主键用于 stderr 转发时区分宿主日志来源（[host_key] 前缀）——
            // 合宿连接是组共享进程，按宿主键归因日志。
            .with_plugin_id(host_key);
        // 卡3 通知面：合宿宿主 stderr 的 `[cohost][member-offline]` 结构性行
        // → 注册的回调（内核据此发系统通知）。未注册回调 = None，行为不变。
        // host.py 只在合宿形态输出该行，观察者只挂合宿客户端。
        if is_cohost_member(manifest) {
            if let Some(observer) = self.member_offline_observer() {
                c = c.with_stderr_notice_observer(observer);
            }
        }

        // 应用 Capability 路由器（启用 sidecar→内核反向调用通道）。
        // PluginScopedRouter 把连接身份注入每次反向调用的 params（metrics
        // 命名空间）。身份 = 进程身份：独占 = manifest.id；合宿 = 宿主键
        // ——身份不随成员重生漂移，指标按宿主聚合稳定。
        {
            let router_guard = self.router.read();
            if let Some(router) = router_guard.as_ref() {
                let scoped: Arc<dyn CapabilityRouter> = Arc::new(PluginScopedRouter::new(
                    scoped_router_identity(manifest, host_key),
                    Arc::clone(router),
                ));
                c = c.with_router(scoped);
            }
        }

        // 设置工作目录（light 合宿 = plugins/shared/_host/；独占 = 插件目录，
        // 确保 server.py 等相对路径可解析）
        if let Some(dir) = working_dir {
            c = c.with_working_dir(dir);
        }

        // PYTHONPATH 不注入：SDK 由 per-plugin venv 的 editable install 解析，
        // env 注入会形成第二套解析路径（版本不同步事故温床）。
        // 这里只透传日志配置 env（进程级常量，适合走 env；per-request 上下文
        // 走 JSON-RPC）。仅当父进程已设置时透传，否则让 sidecar SDK 用其默认
        // （INFO + stderr）。SDK 启动时读这些 env 调用 setup_logging，使 sidecar
        // 日志走统一基础设施。
        let mut extra_env: Vec<(String, String)> = Vec::new();
        for key in &["LOG_LEVEL", "LOG_JSON", "LOG_FORMAT"] {
            if let Ok(val) = std::env::var(key) {
                if !val.is_empty() {
                    extra_env.push(((*key).to_string(), val));
                }
            }
        }
        // venv 直达解析的 venv 身份锚（__PYVENV_LAUNCHER__）走同一 env 注入通道。
        if let Some(pair) = venv_direct_env {
            extra_env.push(pair);
        }
        if !extra_env.is_empty() {
            c = c.with_extra_env(extra_env);
        }
        Ok(c)
    }

    /// 合宿宿主的成员级握手配置映射：`{plugin_id: 该成员 config_files 注入命名空间}`。
    ///
    /// 独占宿主（非组键）返回 None——initialize params 不携带 `member_configs`
    /// 字段，与旧形态字节一致（行为零变化）。组宿主对 [`Self::host_members`]
    /// 的当前装箱成员集逐个 [`shared::build_injected_config`]（全量配置只装载
    /// 一次，[`shared::load_full_config`] 的 PARSE 上抛/IO 降级语义随之继承）；
    /// 触发成员自身同样入映射（与顶层 `config` 同值，路由后语义一致）。成员
    /// manifest 缺失（并发卸载窗口）跳过——该成员在 SDK 侧回落顶层 `config`
    /// （即旧行为），不阻断其余成员。
    pub(super) async fn group_member_configs(
        &self,
        host_key: &str,
    ) -> Result<Option<Value>, PluginError> {
        if parse_group_slot(host_key).is_none() {
            return Ok(None);
        }
        let full_config = crate::shared::load_full_config(self.loader.as_ref()).await?;
        let mut map = serde_json::Map::new();
        for pid in self.host_members(host_key) {
            if let Some(member_manifest) = self.loader.get_manifest(&pid) {
                map.insert(
                    pid,
                    crate::shared::build_injected_config(&full_config, &member_manifest),
                );
            }
        }
        Ok(Some(Value::Object(map)))
    }

    /// 新客户端激活链：connect → 装载配置 → initialize 握手 → on_load 直调 +
    /// 总线旁路广播 → 按宿主键写入缓存并记活跃时刻。成功即返回可复用句柄
    /// （拿走客户端所有权——连接后的进程句柄归宿主键缓存条目所有）。
    pub(super) async fn connect_initialize_and_cache(
        &self,
        mut client: McpClient,
        host_key: &str,
        manifest: &PluginManifest,
    ) -> Result<SharedMcpClient, PluginError> {
        // 启动窗时间界（ADR 2026-09-28）：connect + initialize 全程罩在独立的
        // start-up 超时内，与 request_timeout（默认 86400s）解耦——sidecar 启动
        // 窗挂死时 fail-fast，spawn 锁随 future 结束自然释放；G2 观测/生命周期
        // 钩子/预热/管道流式四条无外层超时路径经本函数同一收口自动有界。
        let start_window = host_start_timeout();
        let started = Instant::now();
        // ADR 2026-10-19 D4 第二刀：宿主 Job 守卫（spawn 成功即在 connect 后
        // 挂载——宿主侧 import/初始化正是分配大头，containment 必须覆盖窗口
        // 起点，不得等到握手完成）。握手失败时本函数返回，守卫随局部变量
        // drop = Job 关闭 = 半开树陪葬（与既有 kill 路径互为兜底）；成功时
        // 在下方缓存写入点随客户端记录登记。
        let mut host_job_guard: Option<HostJobGuard> = None;
        let handshake = async {
            client.connect().await.map_err(|e| PluginError {
                message: format!("MCP connect failed: {e}"),
                code: Some("MCP_CONNECT_FAILED".to_string()),
                source: Some("plugin-invoker".to_string()),
            })?;

            // ADR D4 第二刀：spawn 成功即挂 per-host Job（PROCESS_MEMORY 硬限
            // + KILL_ON_JOB_CLOSE，越限走既有崩溃回调路径，契约见 host_job
            // 模块注释）。best-effort：guard 缺席（限未启用/Assign 失败）不阻断。
            host_job_guard = host_job::contain_spawned_host(
                host_key,
                client.pid().await,
                host_job::host_mem_limit_mb_from_env(),
            );

            // initialize 握手（携带插件配置）。配置加载分级语义（PARSE 上抛/
            // IO 降级空配置）由 shared::injected_config 统一承载。
            // 按需注入（ADR §4.3，P6）：只走 config_files 映射；未声明则收空配置。
            // 避免把全系统配置（含其他插件凭证）泄漏给每个 sidecar。
            // 复用 shared::build_injected_config——native/wasm 分支也走同一函数，三家对齐。
            let config = crate::shared::injected_config(self.loader.as_ref(), manifest).await?;
            // 合宿宿主补成员级注入映射：`config` 只承载触发成员（manifest 参数）
            // 的 config_files 命名空间，单 initialize 参数无成员维度，SDK 合宿扇出
            // 把它发给全部成员——非触发成员的构造期配置落空（2026-10-09 部署实测：
            // 触发成员 agent_config_load 无 config_files → isolation_guard 以空配置
            // 构造 → 注入链断裂 fail-closed 拒全部容器要求型工具）。member_configs
            // 每成员各带自己的命名空间（P6 隔离不放松，不并集不共享），SDK 按成员
            // 路由；独占宿主 None，params 不携带该字段（行为零变化）。
            let member_configs = self.group_member_configs(host_key).await?;
            client
                .initialize_with_member_configs(&config, member_configs.as_ref())
                .await
                .map_err(|e| PluginError {
                    message: format!("MCP initialize failed: {e}"),
                    code: Some("MCP_INIT_FAILED".to_string()),
                    source: Some("plugin-invoker".to_string()),
                })?;
            Ok(config)
        };
        let config = match tokio::time::timeout(start_window, handshake).await {
            Ok(result) => result?,
            Err(_) => {
                let elapsed = started.elapsed();
                // 显式 kill 半开客户端，不靠 drop 拖延（kill_on_drop 仅兜底）：
                // 树杀限时 3s + child.kill 兜底，半开进程不留孤儿；kill 失败仅
                // warn（best-effort，与既有 kill 路径口径一致）。
                if let Err(e) = client.kill().await {
                    warn!(
                        "Host start timeout kill failed (kill_on_drop backstop on drop): host={}, err={}",
                        host_key, e
                    );
                }
                error!(
                    "Host start (connect+initialize handshake) timed out: host={}, window={}s, elapsed={:.1}s — half-open client killed, next call will retry respawn",
                    host_key,
                    start_window.as_secs(),
                    elapsed.as_secs_f32()
                );
                return Err(PluginError {
                    message: format!(
                        "host start timeout: {host_key} handshake did not complete within {}s",
                        start_window.as_secs()
                    ),
                    code: Some("HOST_START_TIMEOUT".to_string()),
                    source: Some("plugin-invoker".to_string()),
                });
            }
        };

        // 发送 on_load 通知——触发 Python 插件的 @plugin.on_load 回调，
        // 初始化插件实例（如 _instance = MyPlugin(config)）。
        // 不等待响应（fire-and-forget）；失败仅 warn 不阻断。
        let _ = client
            .send_notification("notifications/on_load", Some(config.clone()))
            .await
            .inspect_err(|e| warn!("on_load notification failed for {}: {}", manifest.id, e));

        // 旁路广播 OnLoad 给总线订阅者（审计/指标）。与上方 `notifications/on_load`
        // 直调属**同一生命周期事件**：直调是权威路径（通知目标插件初始化），
        // 总线是观察路径（审计日志 / `lifecycle.plugin_load_total` 计数）。
        // sidecar 走到此处即视为"已加载"（连接 + initialize 握手完成），无论直调
        // 通知成功与否——观察层关注的是"插件进程被加载起跑"这一事实。
        // best-effort、非阻塞；未注入总线（`None`，如单测）时无操作，行为不变。
        let bus_guard = self.hook_bus.read();
        if let Some(bus) = bus_guard.as_ref() {
            let mut ctx = HookContext::new();
            ctx.set("plugin_id", json!(manifest.id));
            bus.emit(LifecycleEvent {
                hook: LifecycleHook::OnLoad,
                ctx,
                target: EventTarget::Plugin(manifest.id.clone()),
                ts: SystemTime::now(),
            });
        }

        info!(
            "MCP client connected and initialized: host={}, caller={}",
            host_key, manifest.id
        );

        let client_arc = Arc::new(tokio::sync::RwLock::new(client));

        // 缓存（按宿主键——合宿成员共享同一客户端条目）
        {
            let mut clients = self.mcp_clients.write();
            clients.insert(host_key.to_string(), Arc::clone(&client_arc));
        }
        // Job 守卫随客户端记录登记（同生命周期契约，见 host_jobs 字段注释；
        // 同宿主键残留旧守卫被覆盖写关闭——旧树已死则 no-op，漏杀则兜底）
        if let Some(guard) = host_job_guard {
            self.register_host_job(host_key, guard);
        }
        // 监控 M3：记录宿主 spawn 时刻（进程态轮询的 uptime 源；respawn 覆盖写）
        {
            let mut spawned = self.host_spawned_at.write();
            spawned.insert(host_key.to_string(), Instant::now());
        }
        // B8 轮换记账归零（覆盖写）：计数属进程，respawn 即新进程新计数——旧
        // 条目不跨代（与 host_spawned_at 同款记账口径，见字段注释）。
        self.host_request_counts
            .write()
            .insert(host_key.to_string(), 0);
        // 新 spawn 即"活跃"，记录成员最后调用时刻（空闲软卸载依据，成员粒度记账）
        self.touch_member_last_used(&manifest.id);

        Ok(client_arc)
    }

    /// 解析 entry 字段为 command + args。
    ///
    /// entry 格式：`python3 -m my_plugin` 或 `/usr/bin/python3 server.py`
    pub(super) fn parse_entry(&self, entry: &str) -> Result<(String, Vec<String>), PluginError> {
        let parts: Vec<&str> = entry.split_whitespace().collect();
        if parts.is_empty() {
            return Err(PluginError {
                message: "empty entry".to_string(),
                code: Some("EMPTY_ENTRY".to_string()),
                source: Some("plugin-invoker".to_string()),
            });
        }
        let command = parts[0].to_string();
        let args = parts[1..].iter().map(|s| s.to_string()).collect();
        Ok((command, args))
    }

    /// 解析 sidecar 启动命令（uv 迁移，**单轨 fail-closed**）。
    ///
    /// entry 首词是 PATH 裸 python/python3（含 `.exe`/`.bat`/`.cmd` 扩展）的 Python
    /// sidecar **必须**命中插件目录的 venv 解释器：uv 迁移契约双标志
    /// （**pyproject.toml + `.venv`**）缺任一、或 loader 查不到插件目录 → 直接返回
    /// Err（带可读修复指引），**不再回退 PATH 裸 python**——兜底会
    /// 把"venv 未建"静默糊弄成"裸解释器缺依赖启动即崩/行为漂移"，单轨契约下
    /// 早失败比晚卡死好。
    ///
    /// 不受影响的形态：
    /// - entry 首词非 python（如 `node server.js`）→ 原样（本函数只管 Python sidecar）；
    /// - entry 首词带路径分隔符的显式解释器（如 `/usr/bin/python3`）→ 刻意选择，原样；
    /// - external MCP（streamable_http / 外部 stdio command）/ native dll → 不走本函数。
    ///
    /// venv 解释器以**绝对路径**替代裸命令——SDK/第三方依赖由 venv 内 editable
    /// install 解析（不走 PYTHONPATH env）。绝对路径含路径分隔符与
    /// `.`，天然绕过 mcp 侧 `resolve_windows_command` 的 PATHEXT 探测（该函数对
    /// 带分隔符/扩展名的命令原样返回），不会被误解析。
    pub(super) fn resolve_sidecar_command(
        &self,
        manifest: &PluginManifest,
    ) -> Result<(String, Vec<String>), PluginError> {
        let (mut command, args) = self.parse_entry(&manifest.entry)?;
        if !is_plain_python_command(&command) {
            return Ok((command, args));
        }

        // fail-closed：Python sidecar 必须有 venv（uv 单轨，无 plain 回退轨）。
        let dir = self.loader.get_plugin_dir(&manifest.id).ok_or_else(|| {
            PluginError {
                message: format!(
                    "插件 {} 是 Python sidecar（entry 首词为 PATH 裸 python），但 loader 无法定位其插件目录——\
                     无法解析 venv 解释器（uv 单轨契约：pyproject.toml + .venv 双标志；仅支持 venv 解释器，无 PATH python 回退）",
                    manifest.id
                ),
                code: Some("PLUGIN_DIR_NOT_FOUND".to_string()),
                source: Some("plugin-invoker".to_string()),
            }
        })?;
        let plugin_path = std::path::Path::new(&dir);
        if !plugin_path.join("pyproject.toml").is_file() {
            return Err(PluginError {
                message: format!(
                    "插件 {} 缺 pyproject.toml——uv 单轨契约（pyproject.toml + .venv 双标志）不满足，\
                     Python sidecar 不回退 PATH 裸 python。\
                     修复：为插件补 pyproject.toml（模板见 scripts/migrate_plugins_to_uv.py），\
                     依赖清单参照 docs/working/uv依赖人工确认清单_20260819.md（插件目录 {}）",
                    manifest.id,
                    plugin_path.display()
                ),
                code: Some("PYPROJECT_MISSING".to_string()),
                source: Some("plugin-invoker".to_string()),
            });
        }
        // 解析序：①插件目录 .venv（dev 现行为，恒优先）→ ②用户空间登记处
        // （装机态兜底：打包排除 .venv 且插件目录只读，venv 由 autoprovision
        // 落 `<USER_ROOT>/plugin-venvs/<插件id>`，ADR 2026-09-28）→ 均无则
        // fail-closed（uv 单轨，无 PATH python 回退轨）。
        let (interpreter, from_user_space) = match find_venv_interpreter(plugin_path) {
            Some(interp) => (interp, false),
            None => match find_user_space_venv_interpreter(&manifest.id) {
                Some(interp) => (interp, true),
                None => {
                    return Err(PluginError {
                        message: format!(
                            "插件 {} 的 venv 解释器缺失（插件目录 {} 的 \
                             .venv/Scripts/python.exe 与 .venv/bin/python \
                             均不存在，用户空间登记处 plugin-venvs/{} 亦无）\
                             ——Python sidecar 不回退 PATH 裸 python。\
                             修复：dev 在插件目录下 `uv venv --python 3.12 && \
                             uv pip install -e <repo>/plugins/sdk + 确认清单依赖`，\
                             或 `uv sync --project <插件目录>`；\
                             装机态由内核 autoprovision 重建到用户空间\
                             （uv 缺席/失败见 boot 日志 venv-provision 通道）\
                             （重建口径见 docs/working/插件uv运行时迁移方案_20260819.md）",
                            manifest.id,
                            plugin_path.display(),
                            manifest.id
                        ),
                        code: Some("VENV_INTERPRETER_MISSING".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    })
                }
            },
        };
        if from_user_space {
            info!(
                "[invoker] 插件 {} 插件目录 .venv 缺席，\
                 走用户空间 venv 登记处解释器（装机态兜底）| interpreter={}",
                manifest.id,
                interpreter.display()
            );
        } else {
            info!(
                "[invoker] 插件 {} 走 venv 解释器（uv 单轨）| interpreter={}",
                manifest.id,
                interpreter.display()
            );
        }
        command = interpreter.to_string_lossy().into_owned();
        Ok((command, args))
    }

    // ── 宿主键路由（合宿进程模型 §4.2/§4.5）────────────────────────────

    /// 解析插件的宿主键：合宿成员动态装箱到声明组的组宿主，其余独占。
    ///
    /// 合宿成员首次调用时经 [`Self::assign_light_host`] 装箱（粘性，宿主存活期间
    /// 归属不变，声明组名变更即搬组重装箱）；此后每次调用命中分配表直读。
    /// 独占宿主键纯函数构造，无状态。
    pub(super) fn resolve_host_key(&self, manifest: &PluginManifest) -> String {
        if is_cohost_member(manifest) {
            self.assign_light_host(manifest)
        } else {
            solo_host_key(&manifest.id)
        }
    }

    /// 合宿成员装箱分配宿主键（粘性 + 未满宿主优先 + 溢出开新宿主）。
    ///
    /// 装箱域与槽容量按设备档位取值（P2 运行时接线）：手机档全组折入 light
    /// 单域 + 全成员装入（1-2 宿主）；桌面/云档按声明组分域 + 收敛槽容量 20。
    fn assign_light_host(&self, manifest: &PluginManifest) -> String {
        let tier = agentos_core::device_profile::detect_device_tier();
        let group = manifest.host_group.as_deref().unwrap_or(DEFAULT_HOST_GROUP);
        self.assign_light_host_with(
            &manifest.id,
            cohost_packing_domain(group, tier),
            light_host_max_members(),
        )
    }

    /// 装箱核心逻辑（组名与 max_members 参数化以便单测注入，不读环境变量）。
    ///
    /// 落点规则（§4.5，按声明组分域）：
    /// 1. 分配表已有该插件且宿主键属当前声明组 → 返回既有宿主键（粘性，宿主
    ///    存活期间归属不变）；条目属其他组（manifest 改组名 = 搬组）→ 按新组
    ///    重装箱，旧宿主成员集随条目覆盖而收缩；
    /// 2. 从槽位 1 起找**本组**「当前挂载数 < 上限」的宿主塞入——已 idle GC
    ///    回收的宿主（成员条目已清、计数归 0）自然优先复用，而不是无限开新组；
    /// 3. 本组全部满 → 开本组新槽位（序号 = 组内 next_slot+1，装箱顺序即序号）。
    pub(super) fn assign_light_host_with(
        &self,
        plugin_id: &str,
        group: &str,
        max_members: usize,
    ) -> String {
        let mut packing = self.light_packing.write();
        if let Some(host_key) = packing.assignments.get(plugin_id) {
            let same_group = parse_group_slot(host_key).is_some_and(|(g, _)| g == group);
            if same_group {
                return host_key.clone();
            }
        }
        let mut counts: HashMap<u64, usize> = HashMap::new();
        for host_key in packing.assignments.values() {
            if let Some((g, slot)) = parse_group_slot(host_key) {
                if g == group {
                    *counts.entry(slot).or_default() += 1;
                }
            }
        }
        let group_next_slot = packing.next_slot.get(group).copied().unwrap_or(0);
        for slot in 1..=group_next_slot {
            if counts.get(&slot).copied().unwrap_or(0) < max_members {
                let host_key = group_host_key(group, slot);
                packing
                    .assignments
                    .insert(plugin_id.to_string(), host_key.clone());
                return host_key;
            }
        }
        let new_slot = group_next_slot + 1;
        packing.next_slot.insert(group.to_string(), new_slot);
        let host_key = group_host_key(group, new_slot);
        packing
            .assignments
            .insert(plugin_id.to_string(), host_key.clone());
        host_key
    }

    /// 列出宿主的全部成员 plugin_id（排序后返回，作为稳定契约喂给 spawn/指纹/GC）。
    ///
    /// - light 组宿主：分配表反查（成员集随装箱动态变化，respawn 按当前表重建）；
    /// - 独占宿主：键内嵌的 plugin_id 本身。
    pub(super) fn host_members(&self, host_key: &str) -> Vec<String> {
        if parse_group_slot(host_key).is_some() {
            let packing = self.light_packing.read();
            let mut members: Vec<String> = packing
                .assignments
                .iter()
                .filter(|(_, hk)| hk.as_str() == host_key)
                .map(|(pid, _)| pid.clone())
                .collect();
            members.sort();
            members
        } else {
            host_key
                .strip_prefix("plugin:")
                .map(|pid| vec![pid.to_string()])
                .unwrap_or_default()
        }
    }

    /// light 宿主成员集漂移检测：分配表当前成员集 vs 宿主实有集合记账。
    ///
    /// 返回 true = 不一致（权威有、进程无）：宿主进程当前不能服务那部分调用。
    /// **不再等价于"必须重建"**——修复动作是 [`Self::sync_host_member_set`]
    /// 按权威补齐（`agentos/load_member`），补齐失败才整组 kill 重建
    /// （ADR 2026-10-29）。非 light 宿主恒 false（独占宿主成员集 = 键内
    /// 嵌 plugin_id，spawn 后不变）。
    ///
    /// 不一致仅由两类真实成员集变化构成：
    /// - **新成员装箱**：分配表出现记账没有、且不在暂离集的成员——进程成员集
    ///   在 spawn 时定格，必须载入才能服务新成员（否则 MCP [-32602] tool not
    ///   found）；
    /// - **调用者缺席**：调用者自己不在记账（软卸载成员被调用 = 进程无其服务
    ///   面；按既有语义把它带回进程）。
    ///
    /// 软卸载暂离成员（`pending_rejoin_members`）的缺席**不算漂移**：暂离是
    /// 「期望成员集未变、进程服务面暂时收缩」，其余成员照常复用宿主进程——
    /// 暂离若计入漂移，宿主下次被任意成员调用即整组 reload，成员粒度隔离
    /// 被架空（装机版 2026-10-09 实测：300s 空闲软卸载 → 数秒内整组重启循环）。
    ///
    /// 纯内存比对（无 stat、无锁竞争窗口），不受指纹 TTL 门约束——装箱调用
    /// 本身落在 TTL 窗口内，指纹过期检测会漏（指纹记录的是分配表期望态，
    /// 漂移对指纹不可见）。spawn 时快照与分配表更新同序（先装箱后 spawn），
    /// 快照写入先于客户端入缓存，fast path 判定时快照必已就位。
    ///
    /// 快照缺失（缓存条目存在但无快照）按**漂移**处理（BUG-44 安全网）：
    /// 快照与缓存条目在生产路径同生命周期（spawn 时写入先于入缓存、驱逐时
    /// 成对移除），缺失 = 成员集未知——复用成员集未知的进程正是 BUG-44
    /// T1 复用旧宿主报 MCP [-32602] 的形态，fail-closed 重建（respawn 按
    /// 当前分配表重写快照，自愈有界：一次 respawn 后快照必就位）。
    pub(super) fn light_host_members_drifted(&self, host_key: &str, caller_id: &str) -> bool {
        if parse_group_slot(host_key).is_none() {
            return false;
        }
        let current = self.host_members(host_key);
        match self.spawned_members.read().get(host_key).cloned() {
            Some(spawned) => {
                let pending = self
                    .pending_rejoin_members
                    .read()
                    .get(host_key)
                    .cloned()
                    .unwrap_or_default();
                let new_member_packed = current
                    .iter()
                    .any(|m| !spawned.contains(m) && !pending.contains(m));
                let caller_absent = !spawned.iter().any(|m| m == caller_id);
                if new_member_packed || caller_absent {
                    // 漂移归因（整组重载的直接触发点）：new = 分配表含未 spawn 成员，
                    // caller_absent = 调用方自身不在 spawn 快照。两者修法不同，必须可分。
                    tracing::info!(
                        host = %host_key,
                        caller = %caller_id,
                        new_member_packed,
                        caller_absent,
                        current = ?current,
                        spawned = ?spawned,
                        pending = ?pending,
                        "light host member drift detected"
                    );
                }
                new_member_packed || caller_absent
            }
            None => true,
        }
    }

    /// 宿主成员集按权威补齐（ADR 2026-10-29 合宿成员集单一权威）。
    ///
    /// 成员集权威只有分配表（[`Self::host_members`] 反查）；`spawned_members`
    /// 只是"宿主实有"的协议应答记账。两者不一致不是"进程坏了"，而是"进程少装
    /// 了成员"——对缺失成员逐个发 `agentos/load_member`（幂等、带超时、带应答），
    /// 补齐后进程服务面即等于权威，**宿主进程与其余成员不动**。
    ///
    /// 补齐目标 = 权威有、进程无、且不在暂离集的成员（暂离是刻意收缩，不得借
    /// 补齐回灌）+ 调用者自己缺席时（软卸载成员被调用：旧语义是整组 respawn
    /// 把全部暂离成员一次带回，这里只带回它一个，其余暂离成员继续暂离）。
    ///
    /// 返回 true = 成员集已与权威一致（无需补齐，或补齐成功）。返回 false 的
    /// 情形由调用方回退整组 kill 重建，与旧行为完全一致：
    /// - 非组宿主（无进程内成员面）；
    /// - 记账缺失（进程实有未知，BUG-44 fail-closed）；
    /// - 任一成员补齐失败（旧宿主不认该方法 / 协议错 / 超时）。
    pub(super) async fn sync_host_member_set(&self, host_key: &str, caller_id: &str) -> bool {
        if parse_group_slot(host_key).is_none() {
            return false;
        }
        let Some(spawned) = self.spawned_members.read().get(host_key).cloned() else {
            return false;
        };
        let current = self.host_members(host_key);
        let pending = self
            .pending_rejoin_members
            .read()
            .get(host_key)
            .cloned()
            .unwrap_or_default();
        let mut missing: Vec<String> = current
            .iter()
            .filter(|m| !spawned.contains(m) && !pending.contains(*m))
            .cloned()
            .collect();
        if !spawned.iter().any(|m| m == caller_id) && !missing.iter().any(|m| m == caller_id) {
            missing.push(caller_id.to_string());
        }
        if missing.is_empty() {
            return true;
        }
        for pid in &missing {
            if let Err(e) = self.ensure_group_member_present(pid).await {
                warn!(
                    target: "invoker-hotswap",
                    host = %host_key,
                    plugin_id = %pid,
                    error = %e.message,
                    "成员集补齐失败，回退整组驱逐重建"
                );
                return false;
            }
        }
        // 记账由 `ensure_group_member_present` 的应答分支更新（镜像并入 +
        // 暂离标记撤销），此处不重复写；只补一次指纹基线——进程内容此刻等于
        // 权威，不重取会让成员级热换因指纹表缺项判 `SetChanged`，把刚补齐的
        // 宿主又整组重建掉。
        // fail-closed：应答分支未落账（宿主在补齐窗口内被并发驱逐）不得当成功。
        let acked = self
            .spawned_members
            .read()
            .get(host_key)
            .cloned()
            .unwrap_or_default();
        if let Some(pid) = missing.iter().find(|m| !acked.contains(m)) {
            warn!(
                target: "invoker-hotswap",
                host = %host_key,
                plugin_id = %pid,
                "成员载入应答未见记账，按补齐失败回退整组重建"
            );
            return false;
        }
        self.snapshot_member_fingerprints(host_key);
        // 并集基线同窗刷新：每成员表已等于权威，并集缓存不同步刷新的话，
        // 下一次 fast path 会以失步基线误判 stale（见 refresh 助手注释）。
        self.refresh_union_fingerprint_baseline(host_key);
        info!(
            target: "invoker-hotswap",
            host = %host_key,
            added = ?missing,
            "成员集已按分配表补齐（宿主进程与其余成员不动）"
        );
        true
    }

    /// 已 spawn 宿主的宿主键反查（kill/unload 入口按 plugin_id 进来时用）。
    ///
    /// 优先级：light 分配表条目（manifest 可能已不可得但分配仍在）→ manifest
    /// 判定（light 未分配 = 从未 spawn，无宿主；InProcess 无 sidecar 宿主）→
    /// 独占键兜底（manifest 缺失时保守按独占处理，命中缓存即生效）。
    pub(super) fn existing_host_key_for(&self, plugin_id: &str) -> Option<String> {
        if let Some(host_key) = self
            .light_packing
            .read()
            .assignments
            .get(plugin_id)
            .cloned()
        {
            return Some(host_key);
        }
        match self.loader.get_manifest(plugin_id) {
            Some(m) if is_cohost_member(&m) => None,
            Some(m) if m.host_type != HostType::Sidecar => None,
            _ => Some(solo_host_key(plugin_id)),
        }
    }

    /// 解析合宿宿主 spawn 命令（§4.2 第 2 条：`python host.py --group light
    /// --slot {n} --members {逗号分隔成员列表}`）。
    ///
    /// 返回 (command, args, 工作目录)。工作目录固定为合宿宿主目录
    /// `plugins/shared/_host/`（host.py 与共享 venv 的所在地，由宿主侧任务承载，
    /// 内核只管 spawn 参数契约）；解释器走共享 venv（uv 单轨 fail-closed：
    /// 缺 `.venv` 直接报错，不回退 PATH 裸 python）。
    ///
    /// 宿主目录定位序：①从成员插件目录向上探测（现状语义，仓库侧成员路径行为
    /// 不变）；②探测全空时回退 `fallback_host_dir`（生产传 builtin_group_host_dir()
    /// 即内置根 `AGENTOS_PLUGINS_DIR` 下的 `_host/`）——双源用户副本赢后全组成员
    /// 可能落用户空间，祖先链永不含内置 `_host/`，回退使宿主进程与共享 venv 仍由
    /// 内置仓库侧承载（None = 无回退，探测全空照旧 HOST_DIR_NOT_FOUND）。
    pub(super) fn resolve_group_host_command(
        &self,
        group: &str,
        slot: u64,
        members: &[String],
        fallback_host_dir: Option<&Path>,
    ) -> Result<(String, Vec<String>, String), PluginError> {
        // _host 目录定位：优先从任一成员插件目录向上找含 _host 子目录的祖先
        // （插件目录层级不固定：plugins/shared/<type>/<phase>/<name>）；探测
        // 全空才回退内置根 _host/，命中即 warn 留痕（可观测的路径漂移）。
        let host_dir = members
            .iter()
            .filter_map(|pid| self.loader.get_plugin_dir(pid))
            .find_map(|dir| find_group_host_dir(Path::new(&dir)))
            .or_else(|| {
                let fallback = fallback_host_dir
                    .map(|root| root.join(GROUP_HOST_DIR))
                    .filter(|candidate| candidate.is_dir());
                if let Some(dir) = &fallback {
                    warn!(
                        target: "sidecar",
                        members = ?members,
                        host_dir = %dir.display(),
                        "合宿 _host/ 从成员插件目录向上探测未命中，回退内置根 _host/（全组成员落非内置根场景）"
                    );
                }
                fallback
            })
            .ok_or_else(|| PluginError {
                message: format!(
                    "light 合宿宿主目录 {GROUP_HOST_DIR}/ 未定位到（从成员 {members:?} 插件目录向上探测均未命中）——\
                     host.py 由合宿宿主侧任务承载，请确认 plugins/shared/_host/ 已就位"
                ),
                code: Some("HOST_DIR_NOT_FOUND".to_string()),
                source: Some("plugin-invoker".to_string()),
            })?;
        // 共享 venv 解释器（fail-closed，与 resolve_sidecar_command 的 uv 单轨
        // 同轨；解析序同源：宿主目录 .venv 优先，缺席回退用户空间登记处
        // `plugin-venvs/_host`——装机态宿主 venv 由 autoprovision 重定向重建，
        // ADR 2026-09-28-venv-user-space-provisioning）。
        let (interpreter, from_user_space) = match find_venv_interpreter(&host_dir) {
            Some(interp) => (interp, false),
            None => match find_user_space_venv_interpreter(GROUP_HOST_DIR) {
                Some(interp) => (interp, true),
                None => {
                    return Err(PluginError {
                        message: format!(
                            "light 合宿宿主 {} 的共享 venv 解释器缺失\
                             （探测 {}/.venv/Scripts/python.exe 与 \
                             .venv/bin/python 均不存在，用户空间登记处 \
                             plugin-venvs/{GROUP_HOST_DIR} 亦无）——\
                             共享 venv dev 由宿主侧任务构建，\
                             装机态由内核 autoprovision 重建到用户空间",
                            host_dir.display(),
                            GROUP_HOST_DIR
                        ),
                        code: Some("HOST_VENV_MISSING".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    })
                }
            },
        };
        if from_user_space {
            info!(
                target: "sidecar",
                host_dir = %host_dir.display(),
                interpreter = %interpreter.display(),
                "合宿宿主 .venv 缺席，走用户空间 venv 登记处共享解释器（装机态兜底）"
            );
        }
        let mut sorted_members = members.to_vec();
        sorted_members.sort();
        let args = vec![
            "host.py".to_string(),
            "--group".to_string(),
            group.to_string(),
            "--slot".to_string(),
            slot.to_string(),
            "--members".to_string(),
            sorted_members.join(","),
        ];
        Ok((
            interpreter.to_string_lossy().into_owned(),
            args,
            host_dir.to_string_lossy().into_owned(),
        ))
    }

    /// 合宿宿主指纹：当前成员指纹并集 + 成员集本身（§4.5 第 4 条/§4.6）。
    ///
    /// 成员集排序后逐个并入（plugin_id + 各自目录指纹）；任一成员代码/配置
    /// 变更（单成员指纹变）或成员集变化（新成员加入/移出）都会使宿主指纹变化，
    /// 触发 kill 整宿主 respawn。成员 manifest 不可得（插件已被移除）按指纹 0
    /// 纳入（对齐 resolve_fingerprint 拿不到目录返回 0 的哲学）。
    ///
    /// D2 重构：由 [`Self::current_member_fingerprints`] 的每成员表经
    /// [`union_fingerprint_of`] 合成——并集与每成员表同一次遍历产出，pull 路径
    /// stale 定位（逐成员 diff）与 stale 判定（并集比对）天然同源。
    pub(super) fn host_union_fingerprint(&self, host_key: &str) -> u64 {
        let member_fps = self.current_member_fingerprints(host_key);
        union_fingerprint_of(host_key, &member_fps)
    }

    /// 宿主每成员指纹表（当前磁盘态）：{plugin_id: 插件指纹}（ADR 2026-10-19
    /// D2）。成员 manifest 不可得按 0 纳入（与 [`Self::host_union_fingerprint`]
    /// 同哲学）；键序 = BTreeMap 键序 = [`Self::host_members`] 排序序。
    pub(super) fn current_member_fingerprints(
        &self,
        host_key: &str,
    ) -> std::collections::BTreeMap<String, u64> {
        self.host_members(host_key)
            .into_iter()
            .map(|pid| {
                let fp = self
                    .loader
                    .get_manifest(&pid)
                    .map_or(0, |m| self.resolve_fingerprint(&m));
                (pid, fp)
            })
            .collect()
    }

    /// 记录组宿主 spawn 时刻每成员指纹快照（与 `spawned_members` 快照同点调用，
    /// 见 [`Self::build_sidecar_transport_client`]）。失败路径（命令解析报错）
    /// 不写快照——进程未 spawn，无基准可言。
    pub(super) fn snapshot_member_fingerprints(&self, host_key: &str) {
        let fps = self.current_member_fingerprints(host_key);
        self.host_member_fingerprints
            .write()
            .insert(host_key.to_string(), fps);
    }

    /// 并集指纹基线刷新（与每成员表/镜像记账同一逻辑事件里调用）。
    ///
    /// `fingerprints` 并集缓存、`host_member_fingerprints` 每成员表、
    /// `spawned_members` 镜像三者必须同窗更新：成员集补齐/唤醒并入只更新后
    /// 两者时，并集缓存停留旧基线——下一次 fast path（TTL 1s）以「旧并集 vs
    /// 新成员集」误判 stale，热换 diff 又是 NoChange → 整组 kill+respawn。
    /// 每补齐一个成员陪葬一次整组 = 2026-11-01 真机 P0（合宿重载风暴）根因。
    pub(super) fn refresh_union_fingerprint_baseline(&self, host_key: &str) {
        let fp = self.host_union_fingerprint(host_key);
        self.fingerprints
            .write()
            .insert(host_key.to_string(), (fp, Instant::now()));
    }

    /// 代码/配置面过期检测（TTL 门 + 指纹比对，不含成员集判定）。
    ///
    /// 独占宿主指纹 = 插件自身指纹（现状语义）；light 宿主 = 成员指纹并集。
    /// 首次调用（缓存无记录）写入指纹返回 false（首次走 spawn，必是最新）。
    ///
    /// light 宿主的**成员集漂移检测**不在本函数（见
    /// [`Self::light_host_members_drifted`]，调用方按需前置判定）：漂移是
    /// 纯内存比对、无 stat 开销，且必须在 TTL 门**之前**——指纹记录的是分配表
    /// 期望态，漂移对指纹不可见，靠指纹过期检测会漏。
    pub(super) async fn host_code_stale(&self, host_key: &str, caller: &PluginManifest) -> bool {
        let now = Instant::now();
        // TTL 门 + 指纹比对在同一把读锁下原子完成（计算指纹前先快照缓存）。
        let cached = self.fingerprints.read().get(host_key).cloned();
        let current_fp = |hk: &str| {
            if parse_group_slot(hk).is_some() {
                self.host_union_fingerprint(hk)
            } else {
                self.resolve_fingerprint(caller)
            }
        };
        match cached {
            None => {
                // 首次：写入指纹，不算过期（此时进程刚 spawn，必是最新）
                let fp = current_fp(host_key);
                self.fingerprints
                    .write()
                    .insert(host_key.to_string(), (fp, now));
                false
            }
            Some((old_fp, last_check)) => {
                // TTL 门：未到期直接复用
                if now.duration_since(last_check) < PLUGIN_FINGERPRINT_TTL {
                    return false;
                }
                // TTL 过期：算当前指纹比对
                let new_fp = current_fp(host_key);
                let stale = new_fp != old_fp;
                // 无论是否过期都刷新检测时刻；指纹变了才更新缓存指纹
                let to_store = if stale { new_fp } else { old_fp };
                self.fingerprints
                    .write()
                    .insert(host_key.to_string(), (to_store, now));
                stale
            }
        }
    }
}
