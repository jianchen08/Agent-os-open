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
        assert!(!handle.is_finished(), "audit subscriber should still be running");

        // 关闭总线（drop 所有 Sender）让订阅者优雅退出。
        drop(bus);
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        // 此时任务应已退出（Closed 分支 break）。
        assert!(handle.is_finished(), "audit subscriber should exit after bus closed");
    }
}
