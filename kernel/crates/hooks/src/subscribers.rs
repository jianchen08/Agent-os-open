//! 生命周期事件订阅者——内核自带消费者。
//!
//! 订阅 [`crate::HookEventBus`] 的事件流，提供开箱即用的消费者（审计日志等）。
//! 设计为后台 tokio 任务（spawn 后独立运行），慢消费者 Lagged 自动 warn 恢复。
//!
//! 新增订阅者只需 `bus.subscribe()` 拿 Receiver 后在自己的 spawn 任务里循环 recv——
//! 事件总线 fan-out，互不干扰。

use std::sync::Arc;

use tokio::sync::broadcast::error::RecvError;
use tokio::task::JoinHandle;
use tracing::{info, warn};

use crate::{HookEventBus, LifecycleEvent};

/// 启动审计日志订阅者：把每个生命周期事件以 structured log 记录。
///
/// 审计是内核自带的可观测性消费者——无需修改点对点分发路径，仅靠订阅总线
/// 即可获得全量生命周期事件日志（hook + 目标 + 上下文标签）。
///
/// 慢消费者处理：收到 `Lagged` 时记 warn 后继续（审计绝不应 fatal 拖垮内核）；
/// 总线关闭（所有 Sender 释放）时优雅退出。
///
/// 返回 `JoinHandle` 供调用方管理任务生命周期（生产环境通常丢弃 handle，任务随进程退出）。
pub fn spawn_audit_subscriber(bus: Arc<HookEventBus>) -> JoinHandle<()> {
    let mut rx = bus.subscribe();
    tokio::spawn(async move {
        info!("lifecycle audit subscriber started");
        loop {
            match rx.recv().await {
                Ok(ev) => log_event(&ev),
                Err(RecvError::Lagged(n)) => {
                    warn!(
                        skipped = n,
                        "audit subscriber lagged, some lifecycle events dropped"
                    );
                }
                Err(RecvError::Closed) => {
                    info!("lifecycle event bus closed, audit subscriber exiting");
                    break;
                }
            }
        }
    })
}

/// 单个事件的审计日志输出。
///
/// `run_id`（若上下文带）作为独立字段提升，便于按 run 关联；其余上下文标签
/// 整体以 debug 形式附在 `ctx` 字段，避免逐标签展开污染日志。
fn log_event(ev: &LifecycleEvent) {
    // 注意：tracing 宏中 `target` 是保留指令（设置日志 target），故字段用 `dst`
    // 表达事件目标，避免与 tracing 的 target 指令冲突。
    match ev.ctx.get("run_id") {
        Some(run_id) => info!(
            hook = ?ev.hook,
            dst = ?ev.target,
            run_id = %run_id,
            "lifecycle event"
        ),
        None => info!(
            hook = ?ev.hook,
            dst = ?ev.target,
            "lifecycle event"
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{EventTarget, LifecycleEvent};
    use agentos_core::traits::{HookContext, LifecycleHook};
    use std::time::SystemTime;

    /// 验证订阅者任务能收到 emit 的事件并正常消费（通过 lagged/恢复路径不 fatal）。
    /// 用 tracing 不观察输出，仅验证任务持续运行 + Receiver 不积压致死。
    #[tokio::test]
    async fn audit_subscriber_consumes_events_without_panicking() {
        let bus = Arc::new(HookEventBus::new(32));
        let handle = spawn_audit_subscriber(bus.clone());

        // 发若干事件，订阅者应全部消费（不影响主线程）。
        for h in [
            LifecycleHook::OnPipelineStart,
            LifecycleHook::OnPipelineEnd,
            LifecycleHook::OnError,
            LifecycleHook::OnLoad,
        ] {
            bus.emit(LifecycleEvent {
                hook: h,
                ctx: HookContext::new(),
                target: EventTarget::Engine,
                ts: SystemTime::now(),
            });
        }

        // 给订阅者一点时间消费。
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        // 任务仍在运行（未因消费事件 panic/退出）。
        assert!(
            !handle.is_finished(),
            "audit subscriber should still be running"
        );

        // 关闭总线（drop 所有 Sender）让订阅者优雅退出。
        drop(bus);
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        // 此时任务应已退出（Closed 分支 break）。
        assert!(
            handle.is_finished(),
            "audit subscriber should exit after bus closed"
        );
    }

    // ── 日志捕获夹具：审计订阅者的可观察输出就是 tracing 事件，故断言日志文本 ──

    /// 收集事件 `message` 字段的测试订阅者（不引入 tracing-subscriber 依赖）。
    #[derive(Clone, Default)]
    struct CapturedLogs(Arc<std::sync::Mutex<Vec<String>>>);

    impl CapturedLogs {
        fn messages(&self) -> Vec<String> {
            self.0.lock().unwrap().clone()
        }

        fn contains(&self, needle: &str) -> bool {
            self.messages().iter().any(|m| m.contains(needle))
        }
    }

    struct MsgVisitor<'a>(&'a mut Option<String>);

    impl tracing::field::Visit for MsgVisitor<'_> {
        fn record_debug(&mut self, field: &tracing::field::Field, value: &dyn std::fmt::Debug) {
            if field.name() == "message" {
                *self.0 = Some(format!("{value:?}"));
            }
        }
    }

    struct LogCapture(CapturedLogs);

    impl tracing::Subscriber for LogCapture {
        fn register_callsite(
            &self,
            _metadata: &'static tracing::Metadata<'static>,
        ) -> tracing::subscriber::Interest {
            // always：保证 callsite 常开、warn!/info! 的 format args 真实求值。
            tracing::subscriber::Interest::always()
        }
        fn enabled(&self, _metadata: &tracing::Metadata<'_>) -> bool {
            true
        }
        fn new_span(&self, _span: &tracing::span::Attributes<'_>) -> tracing::Id {
            tracing::Id::from_u64(1)
        }
        fn record(&self, _span: &tracing::Id, _values: &tracing::span::Record<'_>) {}
        fn record_follows_from(&self, _span: &tracing::Id, _follows: &tracing::Id) {}
        fn event(&self, event: &tracing::Event<'_>) {
            let mut msg = None;
            event.record(&mut MsgVisitor(&mut msg));
            if let Some(m) = msg {
                self.0 .0.lock().unwrap().push(m);
            }
        }
        fn enter(&self, _span: &tracing::Id) {}
        fn exit(&self, _span: &tracing::Id) {}
    }

    /// log_event 的 run_id 分支：上下文带 run_id 时作为独立字段输出，
    /// 不带时走通用分支。两者都以 "lifecycle event" 落审计日志。
    #[tokio::test]
    async fn log_event_emits_with_and_without_run_id() {
        let logs = CapturedLogs::default();
        let _guard = tracing::subscriber::set_default(LogCapture(logs.clone()));

        let mut ctx = HookContext::new();
        ctx.set("run_id", serde_json::json!("run-7"));
        log_event(&LifecycleEvent {
            hook: LifecycleHook::OnPipelineStart,
            ctx,
            target: EventTarget::Pipeline("pipe-1".into()),
            ts: SystemTime::now(),
        });
        log_event(&LifecycleEvent {
            hook: LifecycleHook::OnError,
            ctx: HookContext::new(),
            target: EventTarget::Plugin("plug-1".into()),
            ts: SystemTime::now(),
        });

        let msgs = logs.messages();
        assert_eq!(msgs.len(), 2, "两个事件各产生一条审计日志: {msgs:?}");
        assert!(
            msgs.iter().all(|m| m.contains("lifecycle event")),
            "审计日志文案固定: {msgs:?}"
        );
    }

    /// 慢消费者（容量溢出）触发 Lagged 分支：订阅者记 warn 后继续消费，
    /// 不 fatal、不退出，后续事件仍被消费——观察层绝不被积压拖垮。
    #[tokio::test]
    async fn lagged_subscriber_warns_and_keeps_consuming() {
        let logs = CapturedLogs::default();
        let _guard = tracing::subscriber::set_default(LogCapture(logs.clone()));

        let bus = Arc::new(HookEventBus::new(2));
        let handle = spawn_audit_subscriber(bus.clone());
        // 不 await：订阅者尚未被调度，通道（容量 2）先被 20 个事件冲爆。
        for i in 0..20 {
            bus.emit(LifecycleEvent {
                hook: LifecycleHook::OnPipelineStart,
                ctx: HookContext::new(),
                target: EventTarget::Pipeline(format!("pipe-{i}")),
                ts: SystemTime::now(),
            });
        }

        // 让订阅者跑起来：先撞 Lagged，随后继续消费。
        for _ in 0..8 {
            tokio::task::yield_now().await;
        }
        assert!(
            !handle.is_finished(),
            "Lagged 不是 fatal：订阅者必须继续运行"
        );
        assert!(
            logs.contains("lagged"),
            "溢出必须 warn 留痕，实际日志: {:?}",
            logs.messages()
        );

        // 溢出后仍能消费新事件（恢复能力）。
        let before = logs.messages().len();
        bus.emit(LifecycleEvent {
            hook: LifecycleHook::OnPipelineEnd,
            ctx: HookContext::new(),
            target: EventTarget::Engine,
            ts: SystemTime::now(),
        });
        for _ in 0..8 {
            tokio::task::yield_now().await;
        }
        assert!(
            logs.messages().len() > before,
            "Lagged 恢复后应继续记录事件"
        );

        drop(bus);
        for _ in 0..8 {
            tokio::task::yield_now().await;
        }
        assert!(handle.is_finished(), "总线关闭后订阅者应退出");
    }
}
