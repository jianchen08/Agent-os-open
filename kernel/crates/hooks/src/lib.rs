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
pub fn domain_event(name: &str, tags: Vec<(&str, serde_json::Value)>) -> LifecycleEvent {
    let mut ctx = HookContext::new();
    ctx.set("event", serde_json::json!(name));
    for (key, value) in tags {
        ctx.set(key, value);
    }
    LifecycleEvent {
        hook: LifecycleHook::DomainEvent,
        ctx,
        target: EventTarget::Engine,
        ts: SystemTime::now(),
    }
}

/// 命名订阅者（M3 分发模式的消费端）。
///
/// 与 broadcast Receiver 并存的两类订阅形态：Receiver 适合"事件流消费者"
/// （审计/指标后台任务自持游标）；命名订阅者适合"策略订阅者"——表达
/// bail 短路 / waterfall 改写语义（同步小任务，返回值参与分发决策）。
pub trait EventSubscriber: Send + Sync {
    /// 订阅者 id（诊断日志用，不要求唯一）。
    fn id(&self) -> &str;
    /// 处理事件；返回 `Some(event)` 语义由 [`DispatchMode`] 解释：
    /// bail = 真值短路；waterfall = 改写后的事件；emit/parallel/serial 忽略。
    fn on_event(&self, event: LifecycleEvent) -> Option<LifecycleEvent>;
}

/// 事件分发模式（M3，对应 Cordis events.ts 的五种分发语义）。
///
/// - `Emit`：现状语义——broadcast 通道 fan-out，best-effort 非阻塞（返回 None）。
/// - `Parallel`：命名订阅者全部并发执行（std::thread::scope），等全部完成，返回 None。
/// - `Serial`：命名订阅者按注册顺序执行并等待，返回 None。
/// - `Bail`：顺序执行，**首个返回 Some 的订阅者短路**，分发即停（返回该 Some）。
/// - `Waterfall`：链式执行——前一订阅者返回的 Some(event) 替换事件传给下一个，
///   最终返回最后一个事件（无人改写 = 原事件）。
///
/// 设计边界（与 crate 头注释一致）：点对点直调（`invoker.send_lifecycle_hook`）仍是
/// 权威路径；模式化分发是观察/策略层的增强，绝不改变插件钩子的投递语义。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum DispatchMode {
    #[default]
    Emit,
    Parallel,
    Serial,
    Bail,
    Waterfall,
}

/// 生命周期钩子事件总线（多消费者广播）。
///
/// 内部持一条 `tokio::sync::broadcast` 通道：`emit` 写入，每次 `subscribe` 拿到
/// 一个独立 Receiver（独立游标，只收订阅之后的事件）。容量满时 `emit` **不阻塞**——
/// 慢消费者的 Receiver 会在下次 `recv` 收到 [`broadcast::error::RecvError::Lagged`]，
/// 订阅者自行 warn 后继续（绝不可 fatal，观察层不能拖垮内核）。
///
/// M3：另持一张命名订阅者表（[`EventSubscriber`]），经 [`Self::dispatch`] 按
/// [`DispatchMode`] 分发——bail/waterfall 供审计/指标订阅者表达短路与改写语义。
///
/// 共享方式：包在 `Arc<HookEventBus>` 里在引擎与各订阅者间克隆（Sender 内部 Arc 共享）。
pub struct HookEventBus {
    tx: broadcast::Sender<LifecycleEvent>,
    subscribers: std::sync::RwLock<Vec<std::sync::Arc<dyn EventSubscriber>>>,
}

impl HookEventBus {
    /// 创建总线，`capacity` 为广播通道容量。
    ///
    /// 容量权衡：过小→慢订阅者频繁 Lagged 丢事件；过大→内存占用。
    /// 内核启动默认 1024（生命周期事件低频，足够吸收突发）。
    pub fn new(capacity: usize) -> Self {
        // 丢弃初始空 Receiver：订阅者按需 subscribe。
        let (tx, _rx) = broadcast::channel(capacity);
        Self {
            tx,
            subscribers: std::sync::RwLock::new(Vec::new()),
        }
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

    /// 注册命名订阅者（M3）。按注册顺序参与 Serial/Bail/Waterfall 分发。
    pub fn register_subscriber(&self, subscriber: std::sync::Arc<dyn EventSubscriber>) {
        self.subscribers.write().unwrap().push(subscriber);
    }

    /// 按分发模式分发事件（M3）。
    ///
    /// 返回值语义见 [`DispatchMode`]；`Emit` 等价于 [`Self::emit`]（返回 None）。
    /// 订阅者 panic 会向上传播（策略层分发是同步语义，与 broadcast 的
    /// best-effort 观察通道不同——调用方决定容错边界）。
    pub fn dispatch(&self, event: LifecycleEvent, mode: DispatchMode) -> Option<LifecycleEvent> {
        let subs: Vec<std::sync::Arc<dyn EventSubscriber>> = {
            let guard = self.subscribers.read().unwrap();
            guard.clone()
        };
        match mode {
            DispatchMode::Emit => {
                self.emit(event);
                None
            }
            DispatchMode::Parallel => {
                if subs.is_empty() {
                    return None;
                }
                std::thread::scope(|scope| {
                    for s in &subs {
                        let ev = event.clone();
                        scope.spawn(move || {
                            let _ = s.on_event(ev);
                        });
                    }
                });
                None
            }
            DispatchMode::Serial => {
                for s in &subs {
                    let _ = s.on_event(event.clone());
                }
                None
            }
            DispatchMode::Bail => {
                for s in &subs {
                    if let Some(short) = s.on_event(event.clone()) {
                        return Some(short);
                    }
                }
                None
            }
            DispatchMode::Waterfall => {
                let mut current = event;
                for s in &subs {
                    if let Some(rewritten) = s.on_event(current.clone()) {
                        current = rewritten;
                    }
                }
                Some(current)
            }
        }
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

    // ── M3：分发模式 ──

    struct NamedSub {
        id: &'static str,
        behavior: fn(LifecycleEvent) -> Option<LifecycleEvent>,
    }
    impl EventSubscriber for NamedSub {
        fn id(&self) -> &str {
            self.id
        }
        fn on_event(&self, event: LifecycleEvent) -> Option<LifecycleEvent> {
            (self.behavior)(event)
        }
    }
    fn pass(_: LifecycleEvent) -> Option<LifecycleEvent> {
        None
    }
    fn mark(mut e: LifecycleEvent) -> Option<LifecycleEvent> {
        // 改写：给 ctx 打标记（HookContext tags 是 pub map）。
        e.ctx.set("seen_by", serde_json::json!("marker"));
        Some(e)
    }

    #[test]
    fn m3_bail_short_circuits_on_first_some() {
        let bus = HookEventBus::new(8);
        bus.register_subscriber(std::sync::Arc::new(NamedSub {
            id: "a",
            behavior: pass,
        }));
        bus.register_subscriber(std::sync::Arc::new(NamedSub {
            id: "b",
            behavior: mark,
        }));
        // b 返回 Some → bail 短路；若 a 先短路则不会带 marker。
        let out = bus.dispatch(make_event(LifecycleHook::OnError), DispatchMode::Bail);
        assert!(out.is_some(), "bail 应返回首个真值");
        assert!(out.unwrap().ctx.get("seen_by").is_some());
    }

    #[test]
    fn m3_bail_none_when_all_pass() {
        let bus = HookEventBus::new(8);
        bus.register_subscriber(std::sync::Arc::new(NamedSub {
            id: "a",
            behavior: pass,
        }));
        assert!(bus
            .dispatch(make_event(LifecycleHook::OnError), DispatchMode::Bail)
            .is_none());
    }

    #[test]
    fn m3_waterfall_chains_rewrites() {
        let bus = HookEventBus::new(8);
        bus.register_subscriber(std::sync::Arc::new(NamedSub {
            id: "w1",
            behavior: mark,
        }));
        bus.register_subscriber(std::sync::Arc::new(NamedSub {
            id: "w2",
            behavior: mark,
        }));
        let out = bus
            .dispatch(
                make_event(LifecycleHook::OnPipelineStart),
                DispatchMode::Waterfall,
            )
            .expect("waterfall 总返回事件");
        assert_eq!(
            out.ctx.get("seen_by").and_then(|v| v.as_str()),
            Some("marker")
        );
    }

    #[test]
    fn m3_serial_runs_all_and_returns_none() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        let bus = HookEventBus::new(8);
        static COUNT: AtomicUsize = AtomicUsize::new(0);
        struct Counter;
        impl EventSubscriber for Counter {
            fn id(&self) -> &str {
                "counter"
            }
            fn on_event(&self, _: LifecycleEvent) -> Option<LifecycleEvent> {
                COUNT.fetch_add(1, Ordering::SeqCst);
                None
            }
        }
        bus.register_subscriber(std::sync::Arc::new(Counter));
        bus.register_subscriber(std::sync::Arc::new(Counter));
        assert!(bus
            .dispatch(make_event(LifecycleHook::OnLoad), DispatchMode::Serial)
            .is_none());
        assert_eq!(COUNT.load(Ordering::SeqCst), 2, "serial 应执行全部订阅者");
    }

    #[test]
    fn m3_emit_mode_still_broadcasts() {
        let bus = HookEventBus::new(8);
        assert!(bus
            .dispatch(make_event(LifecycleHook::OnLoad), DispatchMode::Emit)
            .is_none());
    }
}
