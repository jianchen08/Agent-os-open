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
//!   闸门默认上限 8（`AGENTOS_MAX_CONCURRENT_RUNS` 可改，0 = 不限流）——不设
//!   上限则全并行、无人排队，优先级无从生效。
//!
//! [来源: docs/decisions/2026-08-15-pipeline-run-chain-serialization.md]

use std::collections::{BTreeMap, HashMap};
use std::future::Future;
use std::sync::{Arc, OnceLock};

/// 排队优先级：0 = 活跃管道（用户当前选中的管道），1 = 其他。
const RANK_ACTIVE: u8 = 0;
const RANK_BACKGROUND: u8 = 1;

/// 全局并发上限默认值。不设上限则跨管道全并行、无人排队，活跃优先策略
/// 无从生效；8 足以让活跃会话永远优先（下一个空槽必给它）而后台仍有余量。
const DEFAULT_MAX_CONCURRENT_RUNS: usize = 8;

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
}

struct ChainEntry {
    /// 代数：自清理时只有链尾仍是自己（gen 匹配）才移除条目，
    /// 防止先完成任务误删后来者刚插入的链尾。
    gen: u64,
    handle: tokio::task::JoinHandle<()>,
}

impl RunChainRegistry {
    /// 创建实例（测试用）；生产走 [`RunChainRegistry::global`]。
    pub fn new(max_concurrent_runs: usize) -> Arc<Self> {
        Arc::new(Self {
            chains: parking_lot::Mutex::new(HashMap::new()),
            gate: Arc::new(AdmissionGate::new(max_concurrent_runs)),
            active_pipeline: parking_lot::Mutex::new(HashMap::new()),
        })
    }

    /// 进程级单例。并发上限：`AGENTOS_MAX_CONCURRENT_RUNS` 未设 → 默认 8
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

    /// 记录用户当前活跃管道（每次派发前调用，排队优先级依据）。
    pub fn note_user_pipeline(&self, user_id: &str, pipeline_id: &str) {
        if user_id.is_empty() || pipeline_id.is_empty() {
            return;
        }
        self.active_pipeline
            .lock()
            .insert(user_id.to_string(), pipeline_id.to_string());
    }

    /// 该组管道是否有在跑或排队的 run（删会话保护用）。
    pub fn has_pending_any(&self, pipelines: &[String]) -> bool {
        let map = self.chains.lock();
        pipelines.iter().any(|p| map.contains_key(p))
    }

    /// 当前有在跑/排队任务的管道数（观测/测试）。
    pub fn active_chain_count(&self) -> usize {
        self.chains.lock().len()
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

    /// 入链执行：同管道 FIFO，跨管道并行，全局闸门按活跃优先放行。
    ///
    /// 防御：空 key 无法构成串行维度，直接 spawn 不入链（链注册表不记条目）。
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
        // spawn 与 insert 必须在同一临界区完成（spawn 同步、不 await），
        // 否则两个并发 enqueue 可能都拿到"空链"而并行执行。
        let mut map = self.chains.lock();
        let (gen, prev) = match map.remove(&key) {
            Some(entry) => (entry.gen + 1, Some(entry.handle)),
            None => (0, None),
        };
        let task_key = key.clone();
        let task = tokio::spawn(async move {
            // ① 同管道前序：Err（前序 panic/中止）忽略——一个崩溃不毒化整条链。
            if let Some(prev) = prev {
                let _ = prev.await;
            }
            // ② 全局闸门：limit=0 直通；设限时活跃管道优先获得槽位。
            let _guard = registry.gate.acquire(rank).await;
            // ③ 业务执行。
            fut.await;
            // ④ 自清理。panic 时跳过（unwind 中 Drop 已释放闸门），残留条目
            //    由下一次 enqueue 的 remove+insert 覆盖，无语义泄漏。
            let mut map = registry.chains.lock();
            if map.get(&task_key).map(|entry| entry.gen) == Some(gen) {
                map.remove(&task_key);
            }
        });
        map.insert(key, ChainEntry { gen, handle: task });
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

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn has_pending_any_reflects_chain_state() {
        let reg = RunChainRegistry::new(0);
        let release = Arc::new(tokio::sync::Notify::new());
        let wait = Arc::clone(&release);
        reg.enqueue("p1", "", async move {
            let _ = wait.notified().await;
        });
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert!(reg.has_pending_any(&["p1".to_string(), "p2".to_string()]));
        assert!(!reg.has_pending_any(&["p3".to_string()]));
        release.notify_one();
        wait_drained(&reg).await;
        assert!(!reg.has_pending_any(&["p1".to_string()]));
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
}
