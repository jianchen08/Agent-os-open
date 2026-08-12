//! 生命周期事件指标订阅者（监控设计 §三 通道1）。
//!
//! 订阅 [`agentos_hooks::HookEventBus`]，按生命周期钩子类型递增内核计数器，
//! 经 `KernelCounters` → `flush_to` → `MetricsAggregator` → Prometheus 导出链路暴露。
//!
//! 计数指标（counter，命名空间 `lifecycle.*`）：
//! - `lifecycle.pipeline_start_total`：OnPipelineStart 累计次数
//! - `lifecycle.pipeline_end_total`：OnPipelineEnd 累计次数
//! - `lifecycle.plugin_load_total`：OnLoad 累计次数
//! - `lifecycle.plugin_error_total`：OnError 累计次数
//!
//! 设计权衡：本订阅者放在 `api` crate（而非 `hooks`）——`KernelCounters` 聚合中心
//! 位于此处，直接持 `Arc<AtomicCounter>` 句柄 inc，零跨 crate 耦合；`hooks` crate
//! 保持轻量（仅依赖 `agentos-core` + `tokio` + `tracing`），不反向依赖 metrics。

use std::sync::Arc;

use tokio::sync::broadcast::error::RecvError;
use tokio::task::JoinHandle;
use tracing::{info, warn};

use agentos_core::traits::LifecycleHook;
use agentos_hooks::HookEventBus;

use super::aggregator::Labels;
use super::counters::{AtomicCounter, KernelCounters};

/// 生命周期指标计数句柄。
///
/// 在共享 `KernelCounters` 注册四个 counter 后返回的 inc 句柄，订阅者持有，
/// 每条事件按 hook 类型 inc 对应句柄。聚合器周期性 `flush_to` 把 cumulative delta
/// 写入聚合器（监控设计 §三 通道1：原子计数器 + 周期拉快照批量 record）。
struct LifecycleMetricsHandles {
    pipeline_start: Arc<AtomicCounter>,
    pipeline_end: Arc<AtomicCounter>,
    plugin_load: Arc<AtomicCounter>,
    plugin_error: Arc<AtomicCounter>,
}

impl LifecycleMetricsHandles {
    /// 在共享 `KernelCounters` 注册四个 counter，返回 inc 句柄。
    fn register(counters: &KernelCounters) -> Self {
        Self {
            pipeline_start: counters
                .register_counter("lifecycle.pipeline_start_total", Labels::new()),
            pipeline_end: counters
                .register_counter("lifecycle.pipeline_end_total", Labels::new()),
            plugin_load: counters
                .register_counter("lifecycle.plugin_load_total", Labels::new()),
            plugin_error: counters
                .register_counter("lifecycle.plugin_error_total", Labels::new()),
        }
    }
}

/// 启动生命周期指标订阅者：订阅事件总线，按钩子类型 inc 对应计数器。
///
/// 计数经 `KernelCounters` 聚合后随既有 flush 链路（`/metrics` Prometheus 导出）暴露。
/// 慢消费者收到 `Lagged` 时 warn 后继续（指标订阅不应 fatal）；总线关闭时优雅退出。
///
/// 返回 `JoinHandle` 供调用方管理任务生命周期。
pub fn spawn_lifecycle_metrics_subscriber(
    bus: Arc<HookEventBus>,
    counters: Arc<KernelCounters>,
) -> JoinHandle<()> {
    let handles = LifecycleMetricsHandles::register(&counters);
    let mut rx = bus.subscribe();
    tokio::spawn(async move {
        info!("lifecycle metrics subscriber started");
        loop {
            match rx.recv().await {
                Ok(ev) => match ev.hook {
                    LifecycleHook::OnPipelineStart => handles.pipeline_start.inc(1),
                    LifecycleHook::OnPipelineEnd => handles.pipeline_end.inc(1),
                    LifecycleHook::OnLoad => handles.plugin_load.inc(1),
                    LifecycleHook::OnError => handles.plugin_error.inc(1),
                    // OnUnload 当前无发射点（见报告 emit 点表），暂不计单独计数，预留。
                    LifecycleHook::OnUnload => {}
                },
                Err(RecvError::Lagged(n)) => {
                    warn!(
                        skipped = n,
                        "lifecycle metrics subscriber lagged, some events dropped"
                    );
                }
                Err(RecvError::Closed) => {
                    info!("lifecycle event bus closed, metrics subscriber exiting");
                    break;
                }
            }
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_core::traits::HookContext;
    use agentos_hooks::{EventTarget, LifecycleEvent};
    use std::time::SystemTime;

    #[tokio::test]
    async fn metrics_subscriber_counts_by_hook_type() {
        let bus = Arc::new(HookEventBus::new(32));
        let counters = Arc::new(KernelCounters::new());
        let handle = spawn_lifecycle_metrics_subscriber(bus.clone(), counters.clone());

        let emit = |bus: &Arc<HookEventBus>, hook: LifecycleHook| {
            bus.emit(LifecycleEvent {
                hook,
                ctx: HookContext::new(),
                target: EventTarget::Engine,
                ts: SystemTime::now(),
            });
        };
        emit(&bus, LifecycleHook::OnPipelineStart);
        emit(&bus, LifecycleHook::OnPipelineStart);
        emit(&bus, LifecycleHook::OnError);

        // 给订阅者时间消费。
        tokio::time::sleep(std::time::Duration::from_millis(80)).await;

        let snap = counters.snapshot();
        let val = |name: &str| {
            snap.iter()
                .find(|s| s.name == name)
                .map(|s| s.value)
                .unwrap_or(0)
        };
        assert_eq!(val("lifecycle.pipeline_start_total"), 2);
        assert_eq!(val("lifecycle.pipeline_end_total"), 0);
        assert_eq!(val("lifecycle.plugin_load_total"), 0);
        assert_eq!(val("lifecycle.plugin_error_total"), 1);

        // 清理：drop 总线让订阅者退出。
        drop(bus);
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        assert!(handle.is_finished());
    }
}
