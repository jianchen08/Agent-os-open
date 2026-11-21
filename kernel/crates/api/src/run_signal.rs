//! Run 停止信号中枢（事件驱动取消）。
//!
//! 旧链路：dispatch_stop / pipeline-executor.suspend* 把 run 置 suspended
//! （落库传输信号）后，llm 插件以 0.5s 间隔轮询 `get_run_status` 感知——
//! 取消延迟下限 = 轮询间隔，且流式全程空转轮询常驻。
//! 新链路：落标即 [`RunSignalHub::signal`]，插件经
//! `pipeline-executor.wait_run_stop` 长轮询单次挂起等待，信号落地即时唤醒；
//! 窗口到点无信号 = run 正常在跑，插件侧重挂下一窗。等待通道故障时插件
//! 降级回旧轮询节奏（best-effort 契约不变，取消感知不阻断流式主流程）。
//!
//! 进程级单例（[`RunChainRegistry::global`] 同款理由）：signal 发起方
//! （EngineDispatcher / KernelCapabilityRouter 是不同实例）与等待方
//! （capability handler）必须共享同一状态。

use std::collections::HashMap;
use std::sync::{Arc, OnceLock};
use std::time::{Duration, Instant};

/// 条目空闲回收阈值：run_id 每轮新建，等待正常收场后条目留存无害（同 run_id
/// 不会复用）；异常路径残留（等待方崩溃/超时未消费）由本阈值在下次访问时兜底回收。
const ENTRY_TTL: Duration = Duration::from_secs(30 * 60);

/// 触发全量清扫的条目数水位：低于该值不做 TTL 扫描（摊销 O(1) 常规开销）。
const SWEEP_WATERMARK: usize = 64;

/// run 停止信号中枢——run_id → 等待者通知器。
pub struct RunSignalHub {
    /// run_id → (通知器, 创建时刻)。`notify_one` 许可语义：signal 先于等待者
    /// 注册到达也不丢（permit 存储）；等待侧以「先建 notified future 再查
    /// 权威状态」消除丢醒窗口（见 wait_run_stop handler 顺序注释）。
    waiters: parking_lot::Mutex<HashMap<String, (Arc<tokio::sync::Notify>, Instant)>>,
    /// run_id → 落标时刻：已发停止信号的 run。用于 state 行已被删除（会话/
    /// 任务删除后）时仍让后续 wait_run_stop 直接返回 suspended——权威状态
    /// 行没了，等待方不能靠 `get_run` 判断，只能靠信号中枢的记忆。
    cancelled: parking_lot::Mutex<HashMap<String, Instant>>,
}

impl RunSignalHub {
    /// 进程级单例。
    pub fn global() -> Arc<Self> {
        static HUB: OnceLock<Arc<RunSignalHub>> = OnceLock::new();
        HUB.get_or_init(|| {
            Arc::new(Self {
                waiters: parking_lot::Mutex::new(HashMap::new()),
                cancelled: parking_lot::Mutex::new(HashMap::new()),
            })
        })
        .clone()
    }

    /// 落标通知：该 run 的停止信号已生效（run_status=suspended 已落库）。
    /// 空 run_id 幂等空转（空 id 不构成等待维度）。
    pub fn signal(&self, run_id: &str) {
        if run_id.is_empty() {
            return;
        }
        {
            let mut cancelled = self.cancelled.lock();
            if cancelled.len() >= SWEEP_WATERMARK {
                cancelled.retain(|_, born| born.elapsed() < ENTRY_TTL);
            }
            cancelled.insert(run_id.to_string(), Instant::now());
        }
        self.entry(run_id).notify_one();
    }

    /// 该 run 是否已发过停止信号（含 state 行已删除的删除后场景）。
    pub fn is_cancelled(&self, run_id: &str) -> bool {
        if run_id.is_empty() {
            return false;
        }
        let mut cancelled = self.cancelled.lock();
        if cancelled.len() >= SWEEP_WATERMARK {
            cancelled.retain(|_, born| born.elapsed() < ENTRY_TTL);
        }
        cancelled.contains_key(run_id)
    }

    /// 占等待位：返回该 run 的通知 future。调用方必须**先本方法再查权威
    /// 状态**——signal 落在两步之间时由 notify_one 的许可存储兜底，不丢醒。
    pub async fn wait(&self, run_id: &str, timeout: Duration) -> bool {
        if run_id.is_empty() {
            return false;
        }
        let notify = self.entry(run_id);
        let notified = notify.notified();
        tokio::time::timeout(timeout, notified).await.is_ok()
    }

    /// 取（或建）run 的通知器；顺带按水位做 TTL 清扫。
    fn entry(&self, run_id: &str) -> Arc<tokio::sync::Notify> {
        let mut waiters = self.waiters.lock();
        if waiters.len() >= SWEEP_WATERMARK {
            waiters.retain(|_, (_, born)| born.elapsed() < ENTRY_TTL);
        }
        waiters
            .entry(run_id.to_string())
            .or_insert_with(|| (Arc::new(tokio::sync::Notify::new()), Instant::now()))
            .0
            .clone()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// signal 先于 wait 到达（许可存储路径）：wait 立即返回 true，不等超时。
    #[tokio::test]
    async fn signal_before_wait_wakes_immediately() {
        let hub = RunSignalHub::global();
        hub.signal("r-signal-first");
        let started = Instant::now();
        let signaled = hub.wait("r-signal-first", Duration::from_secs(5)).await;
        assert!(signaled, "先落信号后等待必须立即唤醒");
        assert!(started.elapsed() < Duration::from_secs(1));
    }

    /// wait 先注册、signal 后落地：等待被即时唤醒（事件驱动主路径）。
    #[tokio::test]
    async fn wait_then_signal_wakes_waiter() {
        let hub = RunSignalHub::global();
        let waiter_hub = hub.clone();
        let waiter =
            tokio::spawn(async move { waiter_hub.wait("r-live", Duration::from_secs(5)).await });
        // 让 waiter 先完成注册（notified future 创建）再落信号。
        tokio::time::sleep(Duration::from_millis(50)).await;
        hub.signal("r-live");
        assert!(waiter.await.unwrap(), "signal 落地必须唤醒在等 waiter");
    }

    /// 无信号到达：等待满超时返回 false（run 正常在跑的语义）。
    #[tokio::test]
    async fn timeout_without_signal_returns_false() {
        let hub = RunSignalHub::global();
        let started = Instant::now();
        let signaled = hub.wait("r-quiet", Duration::from_millis(80)).await;
        assert!(!signaled);
        assert!(started.elapsed() >= Duration::from_millis(80));
    }

    /// 空 run_id：signal 与 wait 均幂等空转（不 panic、恒超时）。
    #[tokio::test]
    async fn empty_run_id_is_inert() {
        let hub = RunSignalHub::global();
        hub.signal("");
        assert!(!hub.wait("", Duration::from_millis(20)).await);
    }

    /// 停止信号记忆：state 行被删除后，wait_run_stop 仍能据中枢记忆返回
    /// suspended（删除场景下权威状态不可读）。
    #[tokio::test]
    async fn signalled_run_stays_cancelled() {
        let hub = RunSignalHub::global();
        assert!(!hub.is_cancelled("r-deleted-pipe"));
        hub.signal("r-deleted-pipe");
        assert!(hub.is_cancelled("r-deleted-pipe"));
        assert!(hub.wait("r-deleted-pipe", Duration::from_millis(20)).await);
        assert!(!hub.is_cancelled(""), "空 id 不构成取消维度");
    }
}
