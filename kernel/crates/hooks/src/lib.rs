//! # Lingxi Hooks — 生命周期钩子事件总线
//!
//! 多消费者广播总线：把内核生命周期事件（OnPipelineStart/OnPipelineEnd/...）
//! 以 fan-out 方式分发给任意数量的订阅者（审计、指标、可观测性扩展）。
//!
//! ## 为什么需要事件总线
//!
//! 原有分发是**点对点**：`engine.dispatch_hook` → `invoker.send_lifecycle_hook`
//! 只通知目标单个插件，内核自身无法观测生命周期事件（无审计、无指标层）。
//! 本 crate 在点对点分发**旁路**接入一条 tokio broadcast 通道：
//! - 既有直调路径**完全保留**（行为不变，仍是权威路径）
//! - 同时把同一事件广播给所有订阅者，供审计日志 / 指标等消费者消费
//!
//! ## 设计要点
//!
//! - 基于 `tokio::sync::broadcast`：多消费者、各自独立游标、慢消费者 Lagged 可恢复
//! - `emit` 同步非阻塞：发送失败（无订阅者 / 容量满）静默忽略——广播为 best-effort，
//!   绝不阻塞或拖慢引擎热路径（点对点直调才是权威路径，不可被观察层拖垮）
//! - `Option<Arc<HookEventBus>>` 注入：引擎构造加 bus 是 Option 注入，测试可不接入，零行为破坏
//!
//! [来源: docs/0.2_rust_plugin_solution.md §2.2 事件总线]

use std::sync::OnceLock;
use std::time::SystemTime;

use agentos_core::traits::{HookContext, LifecycleHook};
use tokio::sync::broadcast;

pub mod subscribers;

pub use subscribers::spawn_audit_subscriber;

/// 生命周期事件的目标对象。
///
/// 区分事件是发给具体插件、整条管道还是内核引擎本身，
/// 供订阅者按维度过滤/聚合（如审计按目标分流，指标按目标打标签）。
#[derive(Debug, Clone)]
pub enum EventTarget {
    /// 单个插件（plugin_id）。
    Plugin(String),
    /// 一条管道执行（pipeline_id / run_id）。
    Pipeline(String),
    /// 内核引擎自身（如 OnPipelineStart 发给 "__engine__"）。
    Engine,
}

/// 一次生命周期事件（广播给所有订阅者的不可变快照）。
///
/// `LifecycleHook` / `HookContext` 复用 `agentos_core` 已有定义，**不重定义**。
#[derive(Debug, Clone)]
pub struct LifecycleEvent {
    /// 钩子类型（复用 agentos_core 的 LifecycleHook）。
    pub hook: LifecycleHook,
    /// 标签化动态上下文（run_id / branch_id / tenant_id 等，ADR ⑨）。
    pub ctx: HookContext,
    /// 事件目标。
    pub target: EventTarget,
    /// 事件产生时刻。
    pub ts: SystemTime,
}

/// 进程级总线单例：域事件发射点（HTTP handler / WS dispatcher）不便穿透
/// AppState 持有总线句柄，启动期 `set_global` 注册一次（与注入 invoker 的
/// 是同一实例）。未注册时 [`global`] 返回 None——观察层静默降级（测试/降级环境）。
static GLOBAL_BUS: OnceLock<std::sync::Arc<HookEventBus>> = OnceLock::new();

/// 注册进程级总线（启动期调用一次；重复调用静默忽略）。
pub fn set_global(bus: std::sync::Arc<HookEventBus>) {
    let _ = GLOBAL_BUS.set(bus);
}

/// 取进程级总线（未注册返回 None）。
pub fn global() -> Option<std::sync::Arc<HookEventBus>> {
    GLOBAL_BUS.get().cloned()
}

/// 构造域事件（hook = [`LifecycleHook::DomainEvent`]）：
/// 事件名放 `ctx["event"]`，附任意标签（session_id/pipeline_id/user_id 等）。
pub fn domain_event(name: &str, tags: Vec<(String, serde_json::Value)>) -> LifecycleEvent {
    let mut ctx = HookContext::new();
    ctx.set("event", serde_json::json!(name));
    for (key, value) in tags {
        ctx.set(key.as_str(), value);
    }
    LifecycleEvent {
        hook: LifecycleHook::DomainEvent,
        ctx,
        target: EventTarget::Engine,
        ts: SystemTime::now(),
    }
}

/// 生命周期钩子事件总线（多消费者广播）。
///
/// 内部持一条 `tokio::sync::broadcast` 通道：`emit` 写入，每次 `subscribe` 拿到
/// 一个独立 Receiver（独立游标，只收订阅之后的事件）。容量满时 `emit` **不阻塞**——
/// 慢消费者的 Receiver 会在下次 `recv` 收到 [`broadcast::error::RecvError::Lagged`]，
/// 订阅者自行 warn 后继续（绝不可 fatal，观察层不能拖垮内核）。
///
/// 共享方式：包在 `Arc<HookEventBus>` 里在引擎与各订阅者间克隆（Sender 内部 Arc 共享）。
pub struct HookEventBus {
    tx: broadcast::Sender<LifecycleEvent>,
}

impl HookEventBus {
    /// 创建总线，`capacity` 为广播通道容量。
    ///
    /// 容量权衡：过小→慢订阅者频繁 Lagged 丢事件；过大→内存占用。
    /// 内核启动默认 1024（生命周期事件低频，足够吸收突发）。
    pub fn new(capacity: usize) -> Self {
        // 丢弃初始空 Receiver：订阅者按需 subscribe。
        let (tx, _rx) = broadcast::channel(capacity);
        Self { tx }
    }

    /// 广播一个生命周期事件给所有订阅者。
    ///
    /// **best-effort、非阻塞**：
    /// - 无订阅者 → `send` 返回 `Err`（通道无接收端），静默忽略，返回 0
    /// - 容量满 → 通道覆盖最旧值腾位，慢订阅者下次 `recv` 收到 `Lagged`
    /// - 正常 → 返回收到该事件的接收者数量
    ///
    /// 绝不 panic / 绝不阻塞——观察层失败不能影响权威的点对点分发路径。
    pub fn emit(&self, event: LifecycleEvent) -> usize {
        self.tx.send(event).unwrap_or(0)
    }

    /// 订阅事件流，返回独立 Receiver。
    ///
    /// 每次调用拿到一个新游标——只收到**订阅之后** emit 的事件。
    /// 慢消费者必须处理 `RecvError::Lagged`（记 warn 后继续 recv）。
    pub fn subscribe(&self) -> broadcast::Receiver<LifecycleEvent> {
        self.tx.subscribe()
    }

    /// 返回内部 Sender 的克隆，供需要直接持有 Sender 注入的测试/扩展场景。
    pub fn handle(&self) -> broadcast::Sender<LifecycleEvent> {
        self.tx.clone()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_core::traits::HookContext;

    /// 构造一个最小可用的测试事件（空上下文、Engine 目标）。
    fn make_event(hook: LifecycleHook) -> LifecycleEvent {
        LifecycleEvent {
            hook,
            ctx: HookContext::new(),
            target: EventTarget::Engine,
            ts: SystemTime::now(),
        }
    }

    // TDD ①：emit 后订阅者收到该事件。
    #[tokio::test]
    async fn emit_single_subscriber_receives() {
        let bus = HookEventBus::new(16);
        let mut rx = bus.subscribe();
        let n = bus.emit(make_event(LifecycleHook::OnPipelineStart));
        assert_eq!(n, 1, "一个订阅者应收到 1 份");
        let got = rx.recv().await.expect("recv should succeed");
        assert_eq!(got.hook, LifecycleHook::OnPipelineStart);
        assert!(matches!(got.target, EventTarget::Engine));
    }

    // TDD ②：多个订阅者都收到（fan-out 广播）。
    #[tokio::test]
    async fn emit_multiple_subscribers_all_receive() {
        let bus = HookEventBus::new(16);
        let mut rx1 = bus.subscribe();
        let mut rx2 = bus.subscribe();
        assert_eq!(bus.emit(make_event(LifecycleHook::OnPipelineEnd)), 2);

        let g1 = rx1.recv().await.unwrap();
        let g2 = rx2.recv().await.unwrap();
        assert_eq!(g1.hook, LifecycleHook::OnPipelineEnd);
        assert_eq!(g2.hook, LifecycleHook::OnPipelineEnd);
    }

    // TDD ③：容量满时 emit 不阻塞，慢订阅者收到 Lagged 并可恢复。
    #[tokio::test]
    async fn slow_subscriber_gets_lagged_then_recovers() {
        let bus = HookEventBus::new(2);
        let mut rx = bus.subscribe();
        // 慢订阅者：先不消费，发射远超容量（验证 emit 不阻塞——10 次 emit 同步立即返回）。
        for _ in 0..10 {
            bus.emit(make_event(LifecycleHook::OnError));
        }
        // 追加一个标记事件，用于验证订阅者最终能恢复并收到它。
        bus.emit(make_event(LifecycleHook::OnPipelineStart));

        let mut saw_lagged = false;
        let mut saw_start = false;
        for _ in 0..32 {
            match rx.recv().await {
                Ok(ev) => {
                    if ev.hook == LifecycleHook::OnPipelineStart {
                        saw_start = true;
                        break;
                    }
                }
                Err(broadcast::error::RecvError::Lagged(_)) => {
                    // 订阅者实际循环里 warn 后继续（不 fatal），此处标记并继续 drain。
                    saw_lagged = true;
                }
                Err(broadcast::error::RecvError::Closed) => break,
            }
        }
        assert!(saw_lagged, "慢订阅者应至少收到一次 Lagged 错误");
        assert!(saw_start, "Lagged 后订阅者应能恢复并收到后续事件");
    }

    // 补充：无订阅者时 emit 不阻塞、不 panic，返回 0。
    #[tokio::test]
    async fn emit_without_subscribers_is_ok() {
        let bus = HookEventBus::new(4);
        let n = bus.emit(make_event(LifecycleHook::OnLoad));
        assert_eq!(n, 0, "无订阅者时返回 0");
    }

    // 补充：handle() 返回的 Sender 可独立 send，订阅者照常收到。
    #[tokio::test]
    async fn handle_sender_delivers() {
        let bus = HookEventBus::new(8);
        let mut rx = bus.subscribe();
        let tx = bus.handle();
        tx.send(make_event(LifecycleHook::OnPipelineStart))
            .expect("send via handle");
        let got = rx.recv().await.unwrap();
        assert_eq!(got.hook, LifecycleHook::OnPipelineStart);
    }
}
