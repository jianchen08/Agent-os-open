//! 管道执行串行化注册表（ADR-2026-08-15）。
//!
//! 三个引擎入口（WS `user_input`、HTTP `/api/v1/chat`、capability
//! `chat.send_message`）共用本注册表，保证：
//!
//! - **同管道严格 FIFO**：后到的 run 先 await 前序 JoinHandle 再执行。消息层
//!   正确性依赖此序——`PipelineStateRegistry` 按 (tenant, pipeline_id) 回写、
//!   messages 表 msg_sequence 递增，并发 run 会互相覆盖。同管道 run 必须经
//!   本注册表链化，不经链直接 spawn 会放任同会话两条消息并发跑。
//! - **跨管道完全并行**：不同 pipeline 的链互不相干，A 会话长运行不堵 B 会话。
//! - **排队优先级（尽力而为）**：前端当前选中的会话管道 = 活跃管道（切换会话
//!   时经 WS `active_thread_changed` 通知，见 `InboundRouter`），在全局并发闸门
//!   处优先获得槽位；后端兜底：未收到通知时以最近一次 user_input 的管道为准。
//!   闸门默认上限 16（`AGENTOS_MAX_CONCURRENT_RUNS` 可改，0 = 不限流）——不设
//!   上限则全并行、无人排队，优先级无从生效。
//! - **并发计数 = 运行态 run 数（2026-11-18 用户裁定确认）**：等待者不占闸门
//!   槽位，闸门满载的派发由调用方降级到管道持久化队列（队列条可见、可编辑、
//!   重启续跑），不产生"排队占并发"的假饱和。
//! - **活跃管道抢占（2026-11-18 用户裁定）**：用户消息必须立即起管道——活跃
//!   管道派发遇闸门满载时，调用方可查询受害候选（运行态背景 run、最新启动
//!   优先、冷却豁免）并 suspend 最合适者腾槽；受害链收尾检测 `preempt_suspend`
//!   标记后自动经既有 resume 语义重入链续跑（步骤级检查点，在途步重试）。
//!
//! [来源: docs/decisions/2026-08-15-pipeline-run-chain-serialization.md]
//! [修订: docs/decisions/2026-11-18-run-gate-preemption-and-queue-visibility.md]

use std::collections::{BTreeMap, HashMap};
use std::future::Future;
use std::sync::{Arc, OnceLock};

/// 排队优先级：0 = 活跃管道（用户当前选中的管道），1 = 其他。
const RANK_ACTIVE: u8 = 0;
const RANK_BACKGROUND: u8 = 1;

/// 全局并发上限默认值。不设上限则跨管道全并行、无人排队，活跃优先策略
/// 无从生效。16（2026-11-18 用户裁定自 8 放大）：上限只约束机器负载面（每
/// run 一个 LLM 流 + 插件 sidecar 消耗），用户会话的响应性由活跃抢占保证、
/// 不再依赖上限余量；后台长任务排队走持久化队列可见化。
const DEFAULT_MAX_CONCURRENT_RUNS: usize = 16;

/// 抢占冷却：被抢占过的管道在此窗口内豁免再次被选为受害候选（防活锁——
/// 用户消息密集时最新启动的背景 run 会被反复掐断重启，永远跑不完）。
const PREEMPT_COOLDOWN: std::time::Duration = std::time::Duration::from_secs(60);

/// 管道执行链注册表——进程级单例（[`RunChainRegistry::global`]）。
///
/// 必须进程级而非 dispatcher 实例级：生产有两个 `EngineDispatcher` 实例
/// （WS 路由一个、ChatSendHandler 一个），实例内状态会漏闸。
pub struct RunChainRegistry {
    /// pipeline_id → 链尾。链序即 FIFO 序；空链时无条目（零空闲成本）。
    chains: parking_lot::Mutex<HashMap<String, ChainEntry>>,
    /// 全局并发闸门（limit=0 不限流）。
    gate: Arc<AdmissionGate>,
    /// user_id → 当前活跃 pipeline_id。
    ///
    /// 权威来源是前端切换会话时的 `active_thread_changed` 通知（用户当前选中
    /// 的管道）；未收到通知时以最近一次 user_input 派发兜底。容量以用户数为
    /// 上界，不做淘汰。
    active_pipeline: parking_lot::Mutex<HashMap<String, String>>,
    /// pipeline_id → 最近一次被抢占时刻（冷却豁免窗口，防活锁）。
    preempt_cooldown: parking_lot::Mutex<HashMap<String, std::time::Instant>>,
}

struct ChainEntry {
    /// 代数：自清理时只有链尾仍是自己（gen 匹配）才移除条目，
    /// 防止先完成任务误删后来者刚插入的链尾。
    gen: u64,
    handle: tokio::task::JoinHandle<()>,
    /// 占链时刻（僵尸条目摘除 WARN 的年龄取证面；监控消费字段）。
    inserted_at: std::time::Instant,
    /// 入链时的排队 rank（抢占受害候选按当前活跃表**重算**，此字段仅留痕，
    /// 不参与判定——死代码豁免即留痕语义的显式声明）。
    #[allow(dead_code)]
    rank: u8,
    /// 派发用户键（受害候选重算活跃 rank / tenant 解析用）。
    user_id: String,
    /// 链任务已过闸门（进入运行态）：受害候选只选真正在跑 run 的链——
    /// 等闸门的链没有可 suspend 的 run 投影，抢它不腾槽。
    admitted: Arc<std::sync::atomic::AtomicBool>,
}

impl RunChainRegistry {
    /// 内部构造器：进程单例 [`RunChainRegistry::global`] 亦经此构造；
    /// 测试经它造受控独立实例（闸门上限逐测试可控）。模块私有：生产侧
    /// 直呼即双注册表漏闸（注册表必须进程级，见类型级注释）。
    fn new(max_concurrent_runs: usize) -> Arc<Self> {
        Arc::new(Self {
            chains: parking_lot::Mutex::new(HashMap::new()),
            gate: Arc::new(AdmissionGate::new(max_concurrent_runs)),
            active_pipeline: parking_lot::Mutex::new(HashMap::new()),
            preempt_cooldown: parking_lot::Mutex::new(HashMap::new()),
        })
    }

    /// 进程级单例。并发上限：`AGENTOS_MAX_CONCURRENT_RUNS` 未设 → 默认 16
    /// （排队策略生效的前提）；设为 "0" → 不限流；设为 N → 上限 N。
    pub fn global() -> Arc<Self> {
        static REG: OnceLock<Arc<RunChainRegistry>> = OnceLock::new();
        REG.get_or_init(|| {
            let limit = std::env::var("AGENTOS_MAX_CONCURRENT_RUNS")
                .ok()
                .and_then(|v| v.parse::<usize>().ok())
                .unwrap_or(DEFAULT_MAX_CONCURRENT_RUNS);
            Self::new(limit)
        })
        .clone()
    }

    /// 测试专用受控实例构造（dispatch 层注入，免动进程单例的闸门上限）。
    #[cfg(test)]
    pub(crate) fn new_for_tests(max_concurrent_runs: usize) -> Arc<Self> {
        Self::new(max_concurrent_runs)
    }

    /// 记录用户当前活跃管道（每次派发前调用，排队优先级依据）。
    pub fn note_user_pipeline(&self, user_id: &str, pipeline_id: &str) {
        if user_id.is_empty() || pipeline_id.is_empty() {
            return;
        }
        self.active_pipeline
            .lock()
            .insert(user_id.to_string(), pipeline_id.to_string());
    }

    /// 当前有在跑/排队任务的管道数（观测/测试）。
    pub fn active_chain_count(&self) -> usize {
        self.chains.lock().len()
    }

    /// 管道是否有在途执行链（派发已受理、尚未到引擎 run 建行的空窗判定用）。
    /// 空 key 恒 false（空 key 不构成串行维度，见 enqueue 防御）。
    pub fn has_chain(&self, pipeline_key: &str) -> bool {
        !pipeline_key.is_empty() && self.chains.lock().contains_key(pipeline_key)
    }

    /// 活跃管道偏好映射条目数（堆栈级诊断面 memory-breakdown 消费；
    /// 容量以用户数为上界，不做淘汰）。
    pub fn active_pipeline_count(&self) -> usize {
        self.active_pipeline.lock().len()
    }

    /// 管道是否为任一用户当前选中的活跃管道（查看态）。
    ///
    /// 消费方 = notify_judge 的「用户正在看 → 跳过通知判定」门（用户裁定
    /// 2026-10-21：人在场时通知无意义，判定 LLM 与打标全跳）。真值来源与
    /// 排队优先级同源（active_thread_changed + 最近派发兜底）。
    pub fn is_pipeline_viewed(&self, pipeline_id: &str) -> bool {
        !pipeline_id.is_empty()
            && self
                .active_pipeline
                .lock()
                .values()
                .any(|p| p == pipeline_id)
    }

    /// 并发闸门快照 (running, waiting)（堆栈级诊断面：排队即卡顿的直读信号）。
    pub fn gate_snapshot(&self) -> (usize, usize) {
        let inner = self.gate.inner.lock();
        (inner.running, inner.waiting.len())
    }

    /// 闸门是否还有空槽（limit=0 不限流恒 true）。
    ///
    /// 派发面据此分流：有空槽走直跑（乐观气泡），满载降级持久化队列（队列条
    /// 可见）或活跃抢占。注意非原子——返回 true 后槽位仍可能被并发派发取走，
    /// 直跑任务会在闸门处短暂等待（rank 保序，下一空槽必给它）。
    pub fn gate_has_capacity(&self) -> bool {
        if self.gate.limit == 0 {
            return true;
        }
        self.gate.inner.lock().running < self.gate.limit
    }

    /// 抢占受害候选：已过闸门（运行态）的背景链，按"最新启动优先"排序
    /// （重跑损失最小）。排除：请求者自身管道、任一用户的活跃查看管道、
    /// 冷却窗口内的最近受害管道（防活锁：密集用户消息反复掐断同一 run）。
    ///
    /// 返回 (pipeline_id, user_id) 候选序列；调用方逐个探测（查运行投影 →
    /// suspend），成功后必须 [`Self::note_preempted`] 登记冷却。
    pub fn preempt_candidates(&self, requester_pipeline: &str) -> Vec<(String, String)> {
        let mut candidates: Vec<(String, String, std::time::Instant)> = {
            let cooldown = self.preempt_cooldown.lock();
            self.chains
                .lock()
                .iter()
                .filter(|(key, entry)| {
                    key.as_str() != requester_pipeline
                        && entry.admitted.load(std::sync::atomic::Ordering::SeqCst)
                        // 活跃 rank 按当前活跃表重算：入链时是背景、用户此刻
                        // 已切过去的管道不得被抢。
                        && self.priority_rank(&entry.user_id, key) == RANK_BACKGROUND
                        && !self
                            .active_pipeline
                            .lock()
                            .values()
                            .any(|p| p.as_str() == key.as_str())
                        && cooldown
                            .get(key.as_str())
                            .is_none_or(|t| t.elapsed() >= PREEMPT_COOLDOWN)
                })
                .map(|(key, entry)| (key.clone(), entry.user_id.clone(), entry.inserted_at))
                .collect()
        };
        // 最新启动优先（重跑损失最小）。
        candidates.sort_by(|a, b| b.2.cmp(&a.2));
        candidates.into_iter().map(|(p, u, _)| (p, u)).collect()
    }

    /// 登记受害管道冷却（抢占成功后调用）；顺带清扫已过期条目防无界增长。
    pub fn note_preempted(&self, pipeline: &str) {
        let mut cooldown = self.preempt_cooldown.lock();
        cooldown.retain(|_, t| t.elapsed() < PREEMPT_COOLDOWN);
        cooldown.insert(pipeline.to_string(), std::time::Instant::now());
    }

    fn priority_rank(&self, user_id: &str, pipeline_key: &str) -> u8 {
        if !user_id.is_empty()
            && self.active_pipeline.lock().get(user_id).map(String::as_str) == Some(pipeline_key)
        {
            RANK_ACTIVE
        } else {
            RANK_BACKGROUND
        }
    }

    /// 该 (用户, 管道) 是否活跃 rank（派发面抢占分流判定用）。
    pub fn is_active_pipeline(&self, user_id: &str, pipeline_key: &str) -> bool {
        self.priority_rank(user_id, pipeline_key) == RANK_ACTIVE
    }

    /// 入链执行：同管道 FIFO，跨管道并行，全局闸门按活跃优先放行。
    ///
    /// 防御：空 key 无法构成串行维度，直接 spawn 不入链（链注册表不记条目）。
    ///
    /// 堆栈级诊断插桩：链任务注册活动槽（"run-chain:{key}"），标签只在既有
    /// 等待点更新（等前序 FIFO / 等闸门 / 派发执行），O(1) 原子操作；任务
    /// 结束守卫自动注销。task-dump 端点据此回答"哪个管道卡在哪个等待点"。
    pub fn enqueue<F>(self: &Arc<Self>, pipeline_key: &str, user_id: &str, fut: F)
    where
        F: Future<Output = ()> + Send + 'static,
    {
        if pipeline_key.is_empty() {
            tokio::spawn(fut);
            return;
        }
        let rank = self.priority_rank(user_id, pipeline_key);
        let registry = Arc::clone(self);
        let key = pipeline_key.to_string();
        let activity =
            agentos_core::task_activity::global_registry().register(&format!("run-chain:{key}"));
        // spawn 与 insert 必须在同一临界区完成（spawn 同步、不 await），
        // 否则两个并发 enqueue 可能都拿到"空链"而并行执行。
        let mut map = self.chains.lock();
        let (gen, prev) = match map.remove(&key) {
            Some(entry) => (entry.gen + 1, Some(entry.handle)),
            None => (0, None),
        };
        let task_key = key.clone();
        let admitted = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let admitted_for_task = Arc::clone(&admitted);
        let task = tokio::spawn(agentos_core::task_activity::scope(activity, async move {
            // ① 同管道前序：Err（前序 panic/中止）忽略——一个崩溃不毒化整条链。
            if prev.is_some() {
                agentos_core::task_activity::set_current_label("waiting prev run (fifo chain)");
            }
            if let Some(prev) = prev {
                let _ = prev.await;
            }
            // ② 全局闸门：limit=0 直通；设限时活跃管道优先获得槽位。
            agentos_core::task_activity::set_current_label(format!(
                "waiting admission gate (rank={rank})"
            ));
            let _guard = registry.gate.acquire(rank).await;
            admitted_for_task.store(true, std::sync::atomic::Ordering::SeqCst);
            // ③ 业务执行。
            agentos_core::task_activity::set_current_label(format!(
                "dispatching pipeline {task_key}"
            ));
            fut.await;
            // ④ 自清理。panic 时跳过（unwind 中 Drop 已释放闸门），残留条目
            //    由下一次 enqueue 的 remove+insert 覆盖，无语义泄漏。
            let mut map = registry.chains.lock();
            if map.get(&task_key).map(|entry| entry.gen) == Some(gen) {
                map.remove(&task_key);
            }
        }));
        map.insert(
            key,
            ChainEntry {
                gen,
                handle: task,
                inserted_at: std::time::Instant::now(),
                rank,
                user_id: user_id.to_string(),
                admitted,
            },
        );
    }

    /// 原子"链空闲即直跑"（ADR-2026-09-11 条件入队）：在链表锁临界区内判断——
    /// 链空闲则 spawn `fut` 占链并返回 true；链忙返回 false，调用方改走持久化
    /// 入队路径。判定与占链同临界区，两个并发派发不会都拿到"空链"并行直跑。
    ///
    /// `fut` 尾部必须自行 pop pending 队列（收走占链竞态窗口内入队的消息），
    /// FIFO 由"直跑先占链、后续 enqueue 排在其后"保证。
    ///
    /// 诊断插桩同 [`Self::enqueue`]：活动槽 "run-chain:{key}"，等待点标签
    /// O(1) 更新，跑完守卫自动注销。
    ///
    /// # Arguments
    /// 与 [`Self::enqueue`] 同语义：同 key 串行、跨 key 并行、gate 按活跃优先放行。
    pub fn try_enqueue_direct<F>(
        self: &Arc<Self>,
        pipeline_key: &str,
        user_id: &str,
        fut: F,
    ) -> bool
    where
        F: Future<Output = ()> + Send + 'static,
    {
        if pipeline_key.is_empty() {
            tokio::spawn(fut);
            return true;
        }
        let rank = self.priority_rank(user_id, pipeline_key);
        let registry = Arc::clone(self);
        let key = pipeline_key.to_string();
        let mut map = self.chains.lock();
        if let Some(existing) = map.get(&key) {
            if existing.handle.is_finished() {
                // 僵尸条目（run 生命周期防线①，ADR
                // 2026-11-17-run-lifecycle-defense-lines）：任务已结束但自清理
                // 未生效（panic unwind 跳过收尾段）——摘除后按链空闲直跑，
                // 否则同会话后续消息误判链忙全部入持久队（监控台账第 177 轮
                // 实证：条目 49min 未释放、消息迟到 33min）。最小补丁：只在
                // 直跑判定面摘僵尸，不动 enqueue 既有清理语义（remove+insert
                // 覆盖已覆盖其路径）。
                let age = existing.inserted_at.elapsed();
                tracing::warn!(
                    target: "run-chain",
                    pipeline = %key,
                    gen = existing.gen,
                    age_secs = age.as_secs(),
                    "链僵尸条目摘除（任务已结束未自清理），本消息改直跑"
                );
                map.remove(&key);
            } else {
                return false;
            }
        }
        // 链空闲：占链 spawn（同 enqueue 机制，gen 从 0 起、无前序）。
        let gen = 0u64;
        let task_key = key.clone();
        let activity =
            agentos_core::task_activity::global_registry().register(&format!("run-chain:{key}"));
        let admitted = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let admitted_for_task = Arc::clone(&admitted);
        let task = tokio::spawn(agentos_core::task_activity::scope(activity, async move {
            agentos_core::task_activity::set_current_label(format!(
                "waiting admission gate (rank={rank})"
            ));
            let _guard = registry.gate.acquire(rank).await;
            admitted_for_task.store(true, std::sync::atomic::Ordering::SeqCst);
            agentos_core::task_activity::set_current_label(format!(
                "dispatching pipeline {task_key}"
            ));
            fut.await;
            // 自清理同 enqueue：panic 时跳过，残留由下一次 enqueue 覆盖。
            let mut map = registry.chains.lock();
            if map.get(&task_key).map(|entry| entry.gen) == Some(gen) {
                map.remove(&task_key);
            }
        }));
        map.insert(
            key,
            ChainEntry {
                gen,
                handle: task,
                inserted_at: std::time::Instant::now(),
                rank,
                user_id: user_id.to_string(),
                admitted,
            },
        );
        true
    }
}

/// 全局并发闸门——limit=0 不限流；设限时按 (优先级, 到达序) 放行。
///
/// 尽力而为语义：被唤醒的等待者重新竞争槽位，新到的活跃请求可能插队；
/// 无法抢占已发起的 LLM 调用，严格抢占式优先级语义虚假，不做。
struct AdmissionGate {
    /// 0 = 不限流。
    limit: usize,
    inner: parking_lot::Mutex<GateInner>,
}

struct GateInner {
    running: usize,
    /// (rank, seq)：rank 升序（活跃优先），同 rank 按到达序。
    waiting: BTreeMap<(u8, u64), Arc<tokio::sync::Notify>>,
    next_seq: u64,
}

impl AdmissionGate {
    fn new(limit: usize) -> Self {
        Self {
            limit,
            inner: parking_lot::Mutex::new(GateInner {
                running: 0,
                waiting: BTreeMap::new(),
                next_seq: 0,
            }),
        }
    }

    async fn acquire(self: &Arc<Self>, rank: u8) -> GateGuard {
        if self.limit == 0 {
            return GateGuard { gate: None };
        }
        loop {
            let notify = {
                let mut inner = self.inner.lock();
                if inner.running < self.limit {
                    inner.running += 1;
                    return GateGuard {
                        gate: Some(Arc::clone(self)),
                    };
                }
                let seq = inner.next_seq;
                inner.next_seq += 1;
                let notify = Arc::new(tokio::sync::Notify::new());
                inner.waiting.insert((rank, seq), Arc::clone(&notify));
                notify
            };
            // Notify permit 语义：release 在 notified().await 注册前发生也不丢唤醒。
            notify.notified().await;
        }
    }

    fn release(&self) {
        let mut inner = self.inner.lock();
        inner.running = inner.running.saturating_sub(1);
        if let Some((_, notify)) = inner.waiting.pop_first() {
            notify.notify_one();
        }
    }
}

/// 闸门守卫——Drop 释放槽位并唤醒队首等待者（panic unwind 时同样生效）。
struct GateGuard {
    gate: Option<Arc<AdmissionGate>>,
}

impl Drop for GateGuard {
    fn drop(&mut self) {
        if let Some(gate) = self.gate.take() {
            gate.release();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::time::Duration;

    type Log = Arc<parking_lot::Mutex<Vec<u32>>>;

    /// 轮询等待链排空（上限 5s，超时 panic 便于定位）。
    async fn wait_drained(reg: &Arc<RunChainRegistry>) {
        for _ in 0..500 {
            if reg.active_chain_count() == 0 {
                return;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        panic!("链 5s 未排空");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn same_key_runs_in_fifo_order_without_overlap() {
        let reg = RunChainRegistry::new(0);
        let log: Log = Arc::new(parking_lot::Mutex::new(Vec::new()));
        let overlap = Arc::new(AtomicUsize::new(0));
        let max_overlap = Arc::new(AtomicUsize::new(0));
        for i in 0..3u32 {
            let log = Arc::clone(&log);
            let overlap = Arc::clone(&overlap);
            let max_overlap = Arc::clone(&max_overlap);
            reg.enqueue("p1", "", async move {
                let cur = overlap.fetch_add(1, Ordering::SeqCst) + 1;
                max_overlap.fetch_max(cur, Ordering::SeqCst);
                tokio::time::sleep(Duration::from_millis(30)).await;
                log.lock().push(i);
                overlap.fetch_sub(1, Ordering::SeqCst);
            });
        }
        wait_drained(&reg).await;
        assert_eq!(*log.lock(), vec![0, 1, 2], "同管道必须严格 FIFO");
        assert_eq!(max_overlap.load(Ordering::SeqCst), 1, "同管道不得重叠执行");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn different_keys_run_concurrently() {
        let reg = RunChainRegistry::new(0);
        // 握手互等：若被串行化，双方永远等不到对方的通知 → 超时失败。
        let a_started = Arc::new(tokio::sync::Notify::new());
        let b_started = Arc::new(tokio::sync::Notify::new());
        let a_wait = Arc::clone(&a_started);
        let b_wait = Arc::clone(&b_started);
        reg.enqueue("pa", "", async move {
            b_wait.notify_one();
            let _ = a_wait.notified().await;
        });
        let a_wait = Arc::clone(&a_started);
        let b_wait = Arc::clone(&b_started);
        reg.enqueue("pb", "", async move {
            a_wait.notify_one();
            let _ = b_wait.notified().await;
        });
        tokio::time::timeout(Duration::from_secs(2), wait_drained(&reg))
            .await
            .expect("跨管道必须并行（串行会握手死锁）");
    }

    /// 通知出队冲刷的空闲判据（批次 5）：has_chain 跟踪在途执行链窗口——
    /// 链在场 = 活跃（押单不打断流）；链完成摘除 = 空闲（立即冲刷）。
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn has_chain_tracks_active_execution_window() {
        let reg = RunChainRegistry::new(0);
        assert!(!reg.has_chain("p-flush"), "空闲（无在途链）");
        let release = Arc::new(tokio::sync::Notify::new());
        let w = Arc::clone(&release);
        reg.enqueue("p-flush", "u1", async move {
            let _ = w.notified().await;
        });
        wait_admitted(&reg, "p-flush").await;
        assert!(reg.has_chain("p-flush"), "在途执行链在场 = 活跃");
        release.notify_one();
        let gone = tokio::time::timeout(std::time::Duration::from_millis(500), async {
            while reg.has_chain("p-flush") {
                tokio::time::sleep(std::time::Duration::from_millis(10)).await;
            }
        })
        .await;
        assert!(gone.is_ok(), "链完成摘除 = 空闲（冲刷窗口开启）");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn panic_does_not_poison_chain() {
        let reg = RunChainRegistry::new(0);
        let log: Log = Arc::new(parking_lot::Mutex::new(Vec::new()));
        reg.enqueue("p1", "", async {
            panic!("前序任务崩溃");
        });
        let log2 = Arc::clone(&log);
        reg.enqueue("p1", "", async move {
            log2.lock().push(1);
        });
        wait_drained(&reg).await;
        assert_eq!(*log.lock(), vec![1], "前序 panic 后链必须继续");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn gate_prioritizes_active_pipeline() {
        let reg = RunChainRegistry::new(1);
        let order: Arc<parking_lot::Mutex<Vec<&'static str>>> =
            Arc::new(parking_lot::Mutex::new(Vec::new()));
        // 用户 u 的活跃管道 = pA
        reg.note_user_pipeline("u", "pA");
        // 占住唯一槽位（用户 v，背景管道 pB）
        let holder_release = Arc::new(tokio::sync::Notify::new());
        let holder_wait = Arc::clone(&holder_release);
        let order1 = Arc::clone(&order);
        reg.enqueue("pB", "v", async move {
            let _ = holder_wait.notified().await;
            order1.lock().push("holder");
        });
        // 等 holder 真正进入闸门（running=1）
        wait_gate_running(&reg, 1).await;
        // 先排背景 pB2（用户 v），再排活跃 pA（用户 u）
        let order2 = Arc::clone(&order);
        reg.enqueue("pB2", "v", async move {
            order2.lock().push("bg");
        });
        let order3 = Arc::clone(&order);
        reg.enqueue("pA", "u", async move {
            order3.lock().push("active");
        });
        // 等两个等待者都注册
        wait_gate_waiting(&reg, 2).await;
        holder_release.notify_one();
        wait_drained(&reg).await;
        assert_eq!(
            *order.lock(),
            vec!["holder", "active", "bg"],
            "释放的槽位必须先给活跃管道"
        );
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn empty_key_bypasses_chain() {
        let reg = RunChainRegistry::new(0);
        reg.enqueue("", "", async {});
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert_eq!(reg.active_chain_count(), 0, "空 key 不入链");
    }

    // ── try_enqueue_direct：链空闲原子直跑（ADR-2026-09-11 条件入队）──

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn try_direct_on_idle_chain_claims_and_runs() {
        let reg = RunChainRegistry::new(0);
        let log: Log = Arc::new(parking_lot::Mutex::new(Vec::new()));
        let log2 = Arc::clone(&log);
        let ran = reg.try_enqueue_direct("p1", "", async move {
            log2.lock().push(7);
        });
        assert!(ran, "链空闲时必须直跑");
        wait_drained(&reg).await;
        assert_eq!(*log.lock(), vec![7], "直跑任务必须被执行");
        assert_eq!(reg.active_chain_count(), 0, "跑完必须自清理占链");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn try_direct_on_busy_chain_returns_false_and_leaves_chain_intact() {
        let reg = RunChainRegistry::new(0);
        let release = Arc::new(tokio::sync::Notify::new());
        let holder_wait = Arc::clone(&release);
        let started = Arc::new(tokio::sync::Notify::new());
        let started_wait = Arc::clone(&started);
        let log: Log = Arc::new(parking_lot::Mutex::new(Vec::new()));
        let log_holder = Arc::clone(&log);
        reg.enqueue("p1", "", async move {
            started_wait.notify_one();
            let _ = holder_wait.notified().await;
            log_holder.lock().push(1);
        });
        started.notified().await; // 占链任务已进入 fut（链非空）
        let ran = reg.try_enqueue_direct("p1", "", async {});
        assert!(!ran, "链忙时必须拒绝直跑交由调用方入队");
        // 链条目未被直跑破坏：释放占链任务后照常执行并排空。
        release.notify_one();
        wait_drained(&reg).await;
        assert_eq!(*log.lock(), vec![1], "原链任务必须不受直跑尝试影响");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn direct_then_enqueue_keeps_fifo_order() {
        let reg = RunChainRegistry::new(0);
        let log: Log = Arc::new(parking_lot::Mutex::new(Vec::new()));
        // T1 直跑占链（慢任务）。
        let log1 = Arc::clone(&log);
        let ran = reg.try_enqueue_direct("p1", "", async move {
            tokio::time::sleep(Duration::from_millis(80)).await;
            log1.lock().push(0);
        });
        assert!(ran);
        // 竞态窗口内 T2 走普通 enqueue：必须排在直跑之后（先到先跑）。
        let log2 = Arc::clone(&log);
        reg.enqueue("p1", "", async move {
            log2.lock().push(1);
        });
        wait_drained(&reg).await;
        assert_eq!(*log.lock(), vec![0, 1], "直跑先占链，后入队必须在其后 FIFO");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn empty_key_try_direct_bypasses_chain() {
        let reg = RunChainRegistry::new(0);
        let ran = reg.try_enqueue_direct("", "", async {});
        assert!(ran, "空 key 无串行维度，照旧直跑");
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert_eq!(reg.active_chain_count(), 0, "空 key 不入链");
    }

    // ── 僵尸条目摘除（run 生命周期防线①，台账第 177 轮）──

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn try_direct_reaps_finished_zombie_and_runs() {
        let reg = RunChainRegistry::new(0);
        let log: Log = Arc::new(parking_lot::Mutex::new(Vec::new()));
        // 构造僵尸：panic 任务——fut.await 展开跳过自清理段，条目残留且 handle
        // 已结束（生产形态：run 结束后条目未释放，台账第 177 轮 49min 僵尸）。
        reg.enqueue("p1", "", async {
            panic!("前序 panic（自清理跳过 → 条目残留为僵尸）");
        });
        // 轮询到僵尸窗口：条目仍在（active==1）且任务已结束（摘除分支命中）。
        let mut reaped = false;
        for _ in 0..500 {
            if reg.active_chain_count() == 1 {
                let log2 = Arc::clone(&log);
                reaped = reg.try_enqueue_direct("p1", "", async move {
                    log2.lock().push(2);
                });
                if reaped {
                    break;
                }
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        assert!(
            reaped,
            "僵尸条目（任务已结束未自清理）必须被摘除并直跑，而非误判链忙入队"
        );
        wait_drained(&reg).await;
        assert_eq!(*log.lock(), vec![2], "摘僵尸后的直跑任务必须被执行");
        assert_eq!(reg.active_chain_count(), 0, "僵尸摘除后的直跑照常自清理");
    }

    /// WARN 取证面：僵尸摘除必须落 WARN 且带条目年龄（监控台账消费字段）。
    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn zombie_reap_warn_carries_entry_age() {
        let reg = RunChainRegistry::new(0);
        reg.enqueue("p-warn", "", async {
            panic!("前序 panic（构造僵尸条目）");
        });
        let buf: Arc<parking_lot::Mutex<Vec<u8>>> = Arc::default();
        let mut reaped = false;
        for _ in 0..500 {
            if reg.active_chain_count() != 1 {
                tokio::time::sleep(Duration::from_millis(10)).await;
                continue;
            }
            let buf_for_writer = Arc::clone(&buf);
            let subscriber = tracing_subscriber::fmt()
                .with_writer(move || TestBufWriter(Arc::clone(&buf_for_writer)))
                .finish();
            let ran = tracing::subscriber::with_default(subscriber, || {
                reg.try_enqueue_direct("p-warn", "", async {})
            });
            if ran {
                reaped = true;
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        assert!(reaped, "僵尸条目必须被摘除");
        let captured = String::from_utf8_lossy(&buf.lock()).into_owned();
        assert!(
            captured.contains("链僵尸条目摘除"),
            "WARN 必须落僵尸摘除行，实际: {captured}"
        );
        assert!(
            captured.contains("age_secs"),
            "WARN 必须携带条目年龄字段（监控消费），实际: {captured}"
        );
    }

    /// 测试日志捕获 writer（`with_writer` 闭包产物，写入共享缓冲）。
    struct TestBufWriter(Arc<parking_lot::Mutex<Vec<u8>>>);

    impl std::io::Write for TestBufWriter {
        fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
            self.0.lock().extend_from_slice(buf);
            Ok(buf.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    async fn wait_gate_running(reg: &Arc<RunChainRegistry>, expect: usize) {
        for _ in 0..500 {
            if reg.gate.inner.lock().running == expect {
                return;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        panic!("闸门 running 未达到 {expect}");
    }

    async fn wait_gate_waiting(reg: &Arc<RunChainRegistry>, expect: usize) {
        for _ in 0..500 {
            if reg.gate.inner.lock().waiting.len() == expect {
                return;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        panic!("闸门等待者未达到 {expect}");
    }

    // ── 堆栈级诊断插桩：等待点标签可见（task-dump 消费面）──

    /// 轮询全局任务注册表，直到 name 条目的标签满足谓词（上限 5s）。
    async fn wait_label(name: &str, pred: impl Fn(&str) -> bool) -> String {
        let reg = agentos_core::task_activity::global_registry();
        for _ in 0..500 {
            if let Some(entry) = reg.snapshot().into_iter().find(|e| e.name == name) {
                if pred(&entry.label) {
                    return entry.label;
                }
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        panic!("任务 {name} 的标签 5s 内未满足谓词");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn chain_task_labels_visible_at_wait_points() {
        let reg = RunChainRegistry::new(1); // 闸门限 1：制造闸门等待
        let name = "run-chain:p-diag";
        let release = Arc::new(tokio::sync::Notify::new());
        let holder_wait = Arc::clone(&release);
        // 占住唯一槽位。
        reg.enqueue("p-holder", "", async move {
            let _ = holder_wait.notified().await;
        });
        wait_gate_running(&reg, 1).await;
        // 被测链任务：入闸前应显形 "waiting admission gate"。
        let entered = Arc::new(tokio::sync::Notify::new());
        let entered_wait = Arc::clone(&entered);
        let ran = reg.try_enqueue_direct("p-diag", "", async move {
            entered_wait.notify_one();
            // 闸门已过：此刻标签应为派发态（等待点标签链路端到端可见）。
            let label = agentos_core::task_activity::current_label()
                .expect("链任务体内标签可读（scope 传播）");
            assert!(
                label.starts_with("dispatching pipeline p-diag"),
                "派发标签实际 {label}"
            );
        });
        assert!(ran);
        let label = wait_label(name, |l| l.contains("waiting admission gate")).await;
        assert!(
            label.contains("rank="),
            "闸门等待标签含优先级（实际 {label}）"
        );
        // 放行 → 派发标签 → 跑完自动注销。
        release.notify_one();
        entered.notified().await;
        wait_drained(&reg).await;
        for _ in 0..500 {
            let alive = agentos_core::task_activity::global_registry()
                .snapshot()
                .into_iter()
                .any(|e| e.name == name);
            if !alive {
                return;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        panic!("链任务跑完必须从 task-dump 注销");
    }

    // ── 闸门容量查询 + 抢占受害候选（ADR 2026-11-18）──

    /// 轮询指定管道链条目过闸（admitted=true），上限 5s。
    async fn wait_admitted(reg: &Arc<RunChainRegistry>, key: &str) {
        for _ in 0..500 {
            let ok = reg
                .chains
                .lock()
                .get(key)
                .is_some_and(|e| e.admitted.load(std::sync::atomic::Ordering::SeqCst));
            if ok {
                return;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        panic!("链 {key} 5s 内未过闸门");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn gate_has_capacity_tracks_running_against_limit() {
        let reg = RunChainRegistry::new(1);
        assert!(reg.gate_has_capacity(), "限 1 空闸必有容量");
        let release = Arc::new(tokio::sync::Notify::new());
        let holder_wait = Arc::clone(&release);
        reg.enqueue("p1", "", async move {
            let _ = holder_wait.notified().await;
        });
        wait_gate_running(&reg, 1).await;
        assert!(!reg.gate_has_capacity(), "槽位占满后必须报无容量");
        release.notify_one();
        wait_drained(&reg).await;
        assert!(reg.gate_has_capacity(), "释放后容量恢复");
        // limit=0 不限流恒有容量。
        let unlimited = RunChainRegistry::new(0);
        let r2 = Arc::clone(&release);
        unlimited.enqueue("px", "", async move {
            let _ = r2.notified().await;
        });
        tokio::time::sleep(Duration::from_millis(30)).await;
        assert!(unlimited.gate_has_capacity(), "不限流恒有容量");
        release.notify_one();
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn preempt_candidates_newest_first_with_exclusions() {
        let reg = RunChainRegistry::new(0);
        let release = Arc::new(tokio::sync::Notify::new());
        // 用户 u1 当前正在查看 p-viewed（活跃查看管道必须豁免）。
        reg.note_user_pipeline("u1", "p-viewed");
        let w1 = Arc::clone(&release);
        reg.enqueue("p-old", "u1", async move {
            let _ = w1.notified().await;
        });
        wait_admitted(&reg, "p-old").await;
        let w2 = Arc::clone(&release);
        reg.enqueue("p-viewed", "u1", async move {
            let _ = w2.notified().await;
        });
        wait_admitted(&reg, "p-viewed").await;
        let w3 = Arc::clone(&release);
        reg.enqueue("p-mid", "u2", async move {
            let _ = w3.notified().await;
        });
        wait_admitted(&reg, "p-mid").await;
        // 请求者 = p-new（自身排除）。候选须排除 p-viewed（活跃查看）与 p-new
        // （请求者），且最新启动优先：p-mid > p-old。
        let candidates = reg.preempt_candidates("p-new");
        assert_eq!(
            candidates,
            vec![
                ("p-mid".to_string(), "u2".to_string()),
                ("p-old".to_string(), "u1".to_string()),
            ],
            "候选必须最新优先且排除请求者/活跃查看管道"
        );
        release.notify_waiters();
        wait_drained(&reg).await;
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn preempt_cooldown_excludes_recent_victim() {
        let reg = RunChainRegistry::new(0);
        let release = Arc::new(tokio::sync::Notify::new());
        let w = Arc::clone(&release);
        reg.enqueue("p-victim", "u1", async move {
            let _ = w.notified().await;
        });
        wait_admitted(&reg, "p-victim").await;
        assert_eq!(
            reg.preempt_candidates("p-other"),
            vec![("p-victim".to_string(), "u1".to_string())]
        );
        reg.note_preempted("p-victim");
        assert!(
            reg.preempt_candidates("p-other").is_empty(),
            "冷却窗口内的受害管道必须豁免"
        );
        release.notify_one();
        wait_drained(&reg).await;
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn preempt_candidates_skip_unadmitted_waiting_chains() {
        let reg = RunChainRegistry::new(1);
        let release = Arc::new(tokio::sync::Notify::new());
        let w = Arc::clone(&release);
        reg.enqueue("p-holder", "u1", async move {
            let _ = w.notified().await;
        });
        wait_gate_running(&reg, 1).await;
        // p-waiting 在闸门外排队（未过闸，无可 suspend 的运行投影）。独立
        // Notify：它此刻还阻塞在闸门上，先于其注册的 notify_waiters 会丢唤醒。
        let release2 = Arc::new(tokio::sync::Notify::new());
        let w2 = Arc::clone(&release2);
        reg.enqueue("p-waiting", "u2", async move {
            let _ = w2.notified().await;
        });
        tokio::time::sleep(Duration::from_millis(50)).await;
        let candidates = reg.preempt_candidates("p-other");
        assert_eq!(
            candidates,
            vec![("p-holder".to_string(), "u1".to_string())],
            "未过闸的等待链不得成为受害候选"
        );
        release.notify_one();
        // holder 释放槽位 → p-waiting 过闸 → 再放行它。
        wait_admitted(&reg, "p-waiting").await;
        release2.notify_one();
        wait_drained(&reg).await;
    }
}
