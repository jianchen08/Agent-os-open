// @feature: FP-0.2.内核内存治理 G2b 在飞请求 GC 随保活池轮换 | @ci: rust-test
//! in-flight 记账与空闲回收：宿主/成员两级 in-flight 计数 RAII guard、spawn
//! 窗口记账 guard 与后台空闲软卸载 GC。
//!
//! 自 invoker.rs 按关注点纯搬运拆出（2026-10-12 文件长度机械闸首个应用案例，
//! ADR docs/decisions/2026-10-12-file-length-mechanical-gate.md）；
//! guards 双 RAII（InflightGuard/StartingPluginsGuard）按"小族合并就近"并入本
//! 模块（消费面即 in-flight/spawn 窗口记账）。
//!
//! ## 残留 pin 兜底（R7 F1，2026-10-15）
//!
//! guard 是 RAII，成功/失败/超时/取消都随 future drop 回落——不存在"漏调
//! release"的代码路径；残留的机制是调用 future 的存活上界即超时链（工具声明
//! timeout_ms > 插件 mcp.request_timeout_secs > 缺省，llm_service=3600s /
//! llm_core=900s / 交互族 86400s）：provider 停滞/fallback 重试链可让 guard
//! 合法悬挂数十分钟，且 GC skip 日志无年龄可见性。兜底 = 创建时刻记账 +
//! GC 门陈旧豁免：宿主全部 guard 超上限且无一属持久保活交互族
//! （lifecycle.idle_timeout_secs=0，wait_for_choice 停泊红线）才放行回收；
//! 正常长任务执行中 guard 必须仍能压住 GC（红线，测试锁定）。

use std::sync::atomic::{AtomicU64, Ordering};

use super::*;

/// 在飞 guard 创建时刻记账条目表（token → (创建时刻, 成员 id)）。
pub(super) type InflightSinceRegistry = Arc<parking_lot::Mutex<HashMap<u64, (Instant, String)>>>;

/// guard 记账 token 发号器（进程内单调，仅用于 registry 键）。
static INFLIGHT_TOKEN: AtomicU64 = AtomicU64::new(0);

/// 残留 pin 豁免上限缺省值（秒）：20min——高于 llm_core 流式自声明界
/// （request_timeout_secs=900s，超时先于本上限触发、guard 自然回落，不误判），
/// 远小于交互族 86400s 界（交互族走保活豁免，不靠本上限）；残留 pin 至多把
/// 回收推迟该时长，不再无限。可用 AGENTOS_INFLIGHT_PIN_MAX_SECS 覆盖（0=关闭
/// 豁免，保守逃生口）。
const DEFAULT_INFLIGHT_PIN_MAX_SECS: u64 = 1200;

/// capability 调用 in-flight guard（RAII）：构造时计数已 +1，`Drop` 时 -1。
///
/// 成功/失败/超时（上层 `tokio::time::timeout` 放弃内层 future）/panic 任何退出
/// 路径都随 future drop 走 `Drop`，计数必然回落（ADR 2026-09-07 P12）。
pub(super) struct InflightGuard {
    counter: Arc<AtomicUsize>,
    /// 创建时刻记账清理句柄（仅 sidecar 统一入口登记，见
    /// [`PluginInvokerImpl::enter_sidecar_inflight`]）：Drop 时摘除自身 token。
    /// 空 = 纯计数 guard（裸宿主面记账），无记账面。
    since_cleanup: Option<(InflightSinceRegistry, u64)>,
}

impl Drop for InflightGuard {
    fn drop(&mut self) {
        self.counter.fetch_sub(1, Ordering::SeqCst);
        if let Some((registry, token)) = self.since_cleanup.take() {
            registry.lock().remove(&token);
        }
    }
}

/// spawn 窗口记账 guard（RAII，D1 宽限面）：构造时成员 id 已写入
/// `starting_plugins`，`Drop` 时成对摘除。成功/失败/panic 任何退出路径都随
/// future drop 走 `Drop`，窗口记账必然出清——泄漏一个条目 = 该插件端点
/// 宽限常开（dispatcher 预算暂停失效），必须出窗即清。
pub(super) struct StartingPluginsGuard<'a> {
    pub(super) set: &'a RwLock<std::collections::HashSet<String>>,
    pub(super) members: Vec<String>,
}

impl Drop for StartingPluginsGuard<'_> {
    fn drop(&mut self) {
        let mut set = self.set.write();
        for member in &self.members {
            set.remove(member);
        }
    }
}

impl PluginInvokerImpl {
    /// 刷新成员的最后调用时刻（调用即"活跃"，重置空闲计时——成员粒度记账）。
    ///
    /// 成员粒度卸载（ADR 2026-09-24-member-granularity-unload-mode-panel-warmup）
    /// 下每个成员按自己的调用时钟独立判定空闲，不再整组续命。
    pub(super) fn touch_member_last_used(&self, plugin_id: &str) {
        self.member_last_used
            .write()
            .insert(plugin_id.to_string(), Instant::now());
    }

    /// 进入一次 capability 调用（裸宿主面记账）：宿主 in-flight 计数 +1，返回
    /// RAII guard；guard drop（成功/失败/超时/panic）时计数 -1（P12，
    /// ADR 2026-09-07）。生产调用方统一走带记账清理句柄的
    /// [`Self::enter_inflight_inner`]；本入口仅 P12 单测消费（无记书面）。
    #[cfg(test)]
    pub(super) fn enter_inflight(&self, host_key: &str) -> InflightGuard {
        self.enter_inflight_inner(host_key, None)
    }

    /// 进入一次 capability 调用的记账内核：sidecar 统一入口携带
    /// 创建时刻记账清理句柄，裸宿主面记账（P12 单测）无记账面。
    fn enter_inflight_inner(
        &self,
        host_key: &str,
        since_cleanup: Option<(InflightSinceRegistry, u64)>,
    ) -> InflightGuard {
        let counter = {
            let mut map = self.inflight_calls.write();
            map.entry(host_key.to_string())
                .or_insert_with(|| Arc::new(AtomicUsize::new(0)))
                .clone()
        };
        counter.fetch_add(1, Ordering::SeqCst);
        InflightGuard {
            counter,
            since_cleanup,
        }
    }

    /// 进入一次 capability 调用：成员 in-flight 计数 +1（成员粒度 GC 门）。
    fn enter_member_inflight(&self, plugin_id: &str) -> InflightGuard {
        let counter = {
            let mut map = self.member_inflight_calls.write();
            map.entry(plugin_id.to_string())
                .or_insert_with(|| Arc::new(AtomicUsize::new(0)))
                .clone()
        };
        counter.fetch_add(1, Ordering::SeqCst);
        InflightGuard {
            counter,
            since_cleanup: None,
        }
    }

    /// sidecar 统一入口 in-flight 计入（P22，ADR 2026-09-07）：
    /// 按 manifest 解析宿主键并 +1（宿主面 + 成员面），返回 (宿主键, 宿主 guard, 成员 guard)。
    ///
    /// 同时登记创建时刻记账（宿主键 registry，token 随宿主 guard Drop 摘除）——
    /// 残留 pin 判定（[`Self::host_inflight_pin_is_residual`]）与 GC skip 日志
    /// 年龄可见性的数据面。
    ///
    /// [`Self::invoke_pipeline_plugin`] / [`Self::invoke_tool`] 的 Sidecar 分支
    /// 是所有 sidecar capability 调用的必经点（管道步骤 / tool-executor 反向
    /// 调用 / http.handle 分发），计数在入口一次持有、覆盖透明恢复全程；
    /// 单次尝试函数（attempt_sidecar_*）不再各自计数。
    pub(super) fn enter_sidecar_inflight(
        &self,
        manifest: &PluginManifest,
    ) -> (String, InflightGuard, InflightGuard) {
        let host_key = self.resolve_host_key(manifest);
        let registry = {
            let mut map = self.inflight_since.write();
            map.entry(host_key.clone())
                .or_insert_with(|| Arc::new(parking_lot::Mutex::new(HashMap::new())))
                .clone()
        };
        let token = INFLIGHT_TOKEN.fetch_add(1, Ordering::Relaxed);
        registry
            .lock()
            .insert(token, (Instant::now(), manifest.id.clone()));
        // B8 轮换记账（ADR 2026-10-19 D1）：宿主累计请求数 +1——本入口是所有
        // sidecar capability 调用的必经点，一次调用一次请求（透明恢复的
        // respawn+重试在 guard 内，不重复计数）。
        self.record_host_request(&host_key);
        let guard = self.enter_inflight_inner(&host_key, Some((registry, token)));
        let member_guard = self.enter_member_inflight(&manifest.id);
        (host_key, guard, member_guard)
    }

    /// 读取宿主当前 in-flight capability 调用数（空闲 GC 回收门，P12）。
    pub(super) fn host_inflight(&self, host_key: &str) -> usize {
        self.inflight_calls
            .read()
            .get(host_key)
            .map(|c| c.load(Ordering::SeqCst))
            .unwrap_or(0)
    }

    /// 读取成员当前 in-flight capability 调用数（成员粒度 GC 回收门）。
    pub(super) fn member_inflight(&self, plugin_id: &str) -> usize {
        self.member_inflight_calls
            .read()
            .get(plugin_id)
            .map(|c| c.load(Ordering::SeqCst))
            .unwrap_or(0)
    }

    /// 残留 pin 豁免上限（秒）：环境变量 AGENTOS_INFLIGHT_PIN_MAX_SECS 覆盖
    /// （0 = 关闭豁免，保守逃生口），缺省 [`DEFAULT_INFLIGHT_PIN_MAX_SECS`]。
    pub(super) fn inflight_pin_ceiling_secs(&self) -> u64 {
        if let Ok(v) = std::env::var("AGENTOS_INFLIGHT_PIN_MAX_SECS") {
            if let Ok(secs) = v.parse::<u64>() {
                return secs;
            }
        }
        DEFAULT_INFLIGHT_PIN_MAX_SECS
    }

    /// 成员是否持久保活交互族（lifecycle.idle_timeout_secs = Some(0)，如
    /// human_interaction 等阻塞等待用户输入型）：其停泊等待（最长达 request
    /// 超时 86400s）无论多久都不得被残留豁免拆散（ADR 2026-09-28 / BUG-81）。
    pub(super) fn is_persist_keepalive_member(&self, plugin_id: &str) -> bool {
        self.loader
            .get_manifest(plugin_id)
            .and_then(|m| m.lifecycle)
            .and_then(|lc| lc.idle_timeout_secs)
            == Some(0)
    }

    /// 宿主最老在飞 guard 的年龄（秒；无记账 None）——GC skip 日志年龄可见性。
    pub(super) fn host_inflight_oldest_age_secs(&self, host_key: &str) -> Option<u64> {
        let registry = self.inflight_since.read().get(host_key).cloned()?;
        let map = registry.lock();
        let now = Instant::now();
        map.values()
            .map(|(since, _)| now.duration_since(*since).as_secs())
            .min()
    }

    /// 宿主在飞 pin 是否已可判定为残留（F1 兜底）：记账非空，且**全部** guard
    /// 满足①创建时刻超上限 ②不属持久保活交互族。任一 guard 不满足（或记账
    /// 缺席/关闭豁免）→ 保守不豁免——正常长任务执行中 guard 必须仍能压住 GC。
    pub(super) fn host_inflight_pin_is_residual(&self, host_key: &str) -> bool {
        let Some(registry) = self.inflight_since.read().get(host_key).cloned() else {
            return false;
        };
        let map = registry.lock();
        if map.is_empty() {
            return false;
        }
        let ceiling = self.inflight_pin_ceiling_secs();
        if ceiling == 0 {
            return false;
        }
        let now = Instant::now();
        map.values().all(|(since, plugin_id)| {
            now.duration_since(*since).as_secs() > ceiling
                && !self.is_persist_keepalive_member(plugin_id)
        })
    }

    /// 启动后台空闲软卸载 GC 任务。
    ///
    /// 每 30s 扫描 last_used（宿主键粒度），对空闲超过阈值的宿主整组回收
    /// （sidecar kill 进程，manifest 描述保留，下次调用重新 spawn）。
    /// 预热集（keep-warm 常驻集，见 [`Self::warmup_sidecar`]）成员豁免回收
    /// ——预热常驻语义，否则预热被架空、首条消息重新全价冷启动。
    /// 对齐 trait 文档声明的「空闲超时自动卸载」设计原则。
    ///
    /// 必须用 Arc<Self> 调用（后台任务需 'static 持有 invoker）。在 main 启动期调一次。
    pub fn start_idle_gc(self: &Arc<Self>) {
        let invoker = Arc::clone(self);
        tokio::spawn(async move {
            // 扫描间隔：30s。比默认 300s 阈值短得多，保证空闲宿主能在阈值后一个周期内被回收。
            let mut interval = tokio::time::interval(Duration::from_secs(30));
            interval.tick().await; // 跳过立即触发的第一次
            loop {
                interval.tick().await;
                invoker.run_idle_gc_pass().await;
            }
        });
        info!("Plugin idle-unload GC task started (scan every 30s)");
    }

    /// 单次 GC 扫描：按成员粒度回收空闲超时的插件（成员级卸载，ADR
    /// 2026-09-24-member-granularity-unload-mode-panel-warmup 取代原 §4.8 整组
    /// 回收的记账面）。
    ///
    /// - 非最后成员：进程内摘除（`unload_group_member`），宿主与其余成员不动；
    /// - 独占宿主 / 组内最后成员：走既有整组回收（kill 进程 + 槽位释放），
    ///   进程级清理兜底不变。
    pub(super) async fn run_idle_gc_pass(&self) {
        // 保活池免杀集合（ADR 2026-10-19 D1）：keepwarm=0（默认）恒空集，本
        // 调用零开销零行为变化；>0 时按 LRU 配额 + TTL 硬顶 + 轮换候选排除
        // 判定（见 host_pool 模块注释）。
        let pool_exempt = self.pool_exempt_hosts_for_pass().await;
        // 快照当前所有"活跃"成员 id，避免长时间持锁
        let candidates: Vec<String> = {
            let mut keys: Vec<String> = self.member_last_used.read().keys().cloned().collect();
            keys.sort();
            keys.dedup();
            keys
        };

        let now = Instant::now();
        for plugin_id in candidates {
            // 预热常驻豁免（成员粒度）：属于预热集的成员自己不回收，其余成员
            // 照常回收——常驻不再连坐同宿主插件；显式卸载（force_unload）不受
            // 此豁免约束。
            if self.keep_warm_plugins.read().contains(&plugin_id) {
                continue;
            }
            let idle_secs = self
                .member_last_used
                .read()
                .get(&plugin_id)
                .map(|t| now.duration_since(*t).as_secs())
                .unwrap_or(0);
            if idle_secs == 0 {
                continue;
            }
            let threshold = self.idle_timeout_secs_sync(&plugin_id);
            // threshold == 0 表示该成员持久保活（声明"永不空闲卸载"，如模式面板包）。
            if threshold == 0 || idle_secs <= threshold {
                continue;
            }
            let Some(host_key) = self.existing_host_key_for(&plugin_id) else {
                // 无宿主（从未 spawn / InProcess / 已卸载）：清掉陈旧记账。
                self.member_last_used.write().remove(&plugin_id);
                continue;
            };
            // 保活池免杀（宿主粒度，ADR 2026-10-19 D1）：配额内且 TTL 未顶且
            // 非轮换候选的宿主不回收——成员软卸载与整组 kill 都跳过，进程与
            // 服务面保持完整（下次调用 fast path 直接复用，冷启归零）。idle
            // 时钟不刷新：LRU 热度与 TTL 照常推进，掉出配额即恢复既有回收。
            if pool_exempt.contains(&host_key) {
                info!(
                    plugin = %plugin_id,
                    host = %host_key,
                    idle_secs = idle_secs,
                    threshold = threshold,
                    "Member idle-unload skipped: host kept warm by pool quota"
                );
                continue;
            }
            // 软卸载 vs 整组回收的判定口径 = **spawn 快照**（进程实际服务面），
            // 不是分配表：成员粒度软卸载保留分配条目（待重加入标记，见
            // finalize_member_unload），表长含「进程已卸、待下次 respawn 回归」
            // 的成员——按表长判定会把「进程内最后一名活跃成员」误走软卸载路径
            // （宿主协议拒绝卸最后成员 → GC 只 warn 不回收，宿主永不释放）。
            // 快照缺失（进程服务面未知）回退分配表口径：生产路径 spawn 先写
            // 快照后入缓存（BUG-44 不变量），缺省仅为异常态兜底，与既有行为
            // 一致。
            let snapshot_len = match self.spawned_members.read().get(&host_key).cloned() {
                Some(spawned) => spawned.len(),
                None => self.host_members(&host_key).len(),
            };
            if snapshot_len > 1 {
                // 宿主在飞门（用户裁定 2026-09-28，ADR 2026-09-28-idle-gc-host-
                // inflight-pin）：成员卸载即变更成员集 → 宿主指纹漂移 → 下一次
                // 任意调用触发整宿主 respawn（BUG-81 语义：旧进程放弃 kill 保留
                // 伺候在飞调用、新进程状态为空）——同宿主上停泊的长等待交互
                // （wait_for_choice）被拆成「等待方旧进程 / 响应方新进程」，用户
                // 响应永远送达不了等待。故宿主有任何在飞调用时任何成员都不得
                // 软卸载；候选成员空闲，延后到宿主排空的下一轮 GC 再卸，代价为
                // 零。候选成员自己的调用也计入宿主面（enter_sidecar_inflight
                // 双面计数），无需单独门。
                // 残留豁免（F1 兜底）：全部 guard 超上限且非保活交互族 → 不再
                // 无限等待，error 强观测后放行回收。
                let host_inflight = self.host_inflight(&host_key);
                let pin_residual =
                    host_inflight > 0 && self.host_inflight_pin_is_residual(&host_key);
                if host_inflight > 0 && !pin_residual {
                    info!(
                        plugin = %plugin_id,
                        host = %host_key,
                        inflight = host_inflight,
                        oldest_guard_age_secs = ?self.host_inflight_oldest_age_secs(&host_key),
                        idle_secs = idle_secs,
                        threshold = threshold,
                        "Member idle-unload skipped: host capability calls in flight"
                    );
                    continue;
                }
                if pin_residual {
                    tracing::error!(
                        plugin = %plugin_id,
                        host = %host_key,
                        inflight = host_inflight,
                        oldest_guard_age_secs = ?self.host_inflight_oldest_age_secs(&host_key),
                        ceiling_secs = self.inflight_pin_ceiling_secs(),
                        "Residual in-flight pin exceeds ceiling: bypassing idle-GC host pin \
                         (residual guard must not defer idle GC indefinitely)"
                    );
                }
                info!(
                    plugin = %plugin_id,
                    host = %host_key,
                    idle_secs = idle_secs,
                    threshold = threshold,
                    "Member idle-unloading (soft): exceeds idle timeout"
                );
                if let Err(e) = self.unload_group_member(&plugin_id).await {
                    warn!("idle GC member unload {} failed: {}", plugin_id, e);
                }
                continue;
            }
            // 独占宿主 / 组内最后成员：整组回收（kill 进程 + 槽位释放）。
            // in-flight 门（P12，ADR 2026-09-07）：有调用在飞不回收；残留豁免
            // 同上（F1 兜底）。
            let inflight = self.host_inflight(&host_key);
            let pin_residual = inflight > 0 && self.host_inflight_pin_is_residual(&host_key);
            if inflight > 0 && !pin_residual {
                info!(
                    host = %host_key,
                    members = ?self.host_members(&host_key),
                    inflight = inflight,
                    oldest_guard_age_secs = ?self.host_inflight_oldest_age_secs(&host_key),
                    idle_secs = idle_secs,
                    threshold = threshold,
                    "Host idle-unload skipped: capability calls in flight"
                );
                continue;
            }
            if pin_residual {
                tracing::error!(
                    host = %host_key,
                    inflight = inflight,
                    oldest_guard_age_secs = ?self.host_inflight_oldest_age_secs(&host_key),
                    ceiling_secs = self.inflight_pin_ceiling_secs(),
                    "Residual in-flight pin exceeds ceiling: bypassing idle-GC host pin \
                     (residual guard must not defer idle GC indefinitely)"
                );
            }
            info!(
                host = %host_key,
                members = ?self.host_members(&host_key),
                idle_secs = idle_secs,
                threshold = threshold,
                "Host idle-unloading (soft): exceeds idle timeout"
            );
            if let Err(e) = self.unload_host(&host_key, true).await {
                warn!("idle GC unload {} failed: {}", host_key, e);
            }
        }
    }

    /// idle_timeout_secs 的同步包装（run_idle_gc_pass 已在 async 上下文，但 load 是 async，
    /// 这里用同步读缓存的方式避免嵌套 await 复杂度——阈值取默认或环境变量即可，精确性非关键）。
    ///
    /// 优先级：插件 manifest 的 lifecycle.idle_timeout_secs > 全局环境变量 > 默认 300s。
    /// 返回 0 表示该插件声明"永不空闲卸载"。
    pub(super) fn idle_timeout_secs_sync(&self, plugin_id: &str) -> u64 {
        // 1) 插件 manifest 声明优先——让每个插件自定义生命周期。
        //    Some(0) = 永不空闲卸载（human_interaction 等阻塞型工具等待用户输入）。
        if let Some(secs) = self
            .loader
            .get_manifest(plugin_id)
            .and_then(|m| m.lifecycle)
            .and_then(|lc| lc.idle_timeout_secs)
        {
            return secs;
        }
        // 2) 全局环境变量覆盖默认
        if let Ok(v) = std::env::var("AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS") {
            if let Ok(secs) = v.parse::<u64>() {
                if secs > 0 {
                    return secs;
                }
            }
        }
        // 3) 默认 300s
        agentos_core::traits::default_idle_timeout()
    }
}
