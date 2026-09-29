// @feature: FP-0.2.可观测性 任务活动记录(task-dump 数据源) | @ci: rust-test
//! 任务活动标签注册表——堆栈级诊断面：「哪个任务此刻在干什么」。
//!
//! ## 为什么需要它
//!
//! RSS 面板只回答"涨了多少"，回答不了"哪个任务卡在哪"。内核的异步任务
//! （定时清扫循环 / 管道执行链 / WS 连接任务 / 派发任务）没有统一的活动
//! 披露面，卡死点（如停泊等待交互响应）只能靠日志推断。本模块给每个任务
//! 一个轻量活动槽：任务启动时注册、进入关键等待点前 O(1) 原子更新标签，
//! 诊断端点（GET /api/v1/system/task-dump）枚举全部任务名 + 当前标签 +
//! 存活时长。
//!
//! ## 插桩约束（等价轻量槽：ArcSwap 承载标签）
//!
//! - **只在既有等待点更新**：不新建锁、不新建通道、不改变等待语义——
//!   [`TaskActivity::set_label`] 是一次 `ArcSwap::store`（单指针原子交换，
//!   无 CAS 循环、无阻塞），可在任意 await 前后调用。
//! - **深层插桩经任务本地槽传播**：api 层（run chain）用 [`scope`] 包住
//!   业务 future，engine 等深层在既有等待点调 [`set_current_label`]，
//!   无槽（非插桩任务）时零成本 no-op——层级间不需要穿针引线传参。
//! - **标签只是诊断陈述**：不参与任何控制流，写坏/漏写不影响正确性。

use arc_swap::ArcSwap;
use std::collections::BTreeMap;
use std::future::Future;
use std::sync::{Arc, OnceLock};
use std::time::Instant;

tokio::task_local! {
    /// 当前任务的活动槽（[`scope`] 写入，[`set_current_label`] 读取）。
    static CURRENT_SLOT: Arc<TaskActivity>;
}

/// 单个任务的活动槽：注册即记起点，标签随等待点演进。
pub struct TaskActivity {
    name: String,
    registered_at: Instant,
    label: ArcSwap<String>,
}

impl TaskActivity {
    fn new(name: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            registered_at: Instant::now(),
            label: ArcSwap::from_pointee(String::from("starting")),
        }
    }

    /// 任务名（注册名，进程内唯一——同名重注册为换代覆盖）。
    pub fn name(&self) -> &str {
        &self.name
    }

    /// 存活秒数（注册至今）。
    pub fn age_secs(&self) -> u64 {
        self.registered_at.elapsed().as_secs()
    }

    /// 当前标签快照。
    pub fn label(&self) -> String {
        self.label.load_full().to_string()
    }

    /// 更新活动标签。O(1)：一次短分配 + 单指针原子交换（无锁、无 CAS 环），
    /// 只在既有等待点调用。
    pub fn set_label(&self, label: impl AsRef<str>) {
        self.label.store(Arc::new(label.as_ref().to_owned()));
    }
}

/// [`TaskActivityRegistry::snapshot`] 的条目。
#[derive(Debug, Clone, serde::Serialize)]
pub struct TaskDumpEntry {
    pub name: String,
    pub label: String,
    pub age_secs: u64,
}

/// 任务活动注册表：任务名 → 活动槽。
///
/// BTreeMap 使 dump 输出按名字稳定排序（诊断读面）。注册/注销只在任务
/// 生命周期边界发生（低频），读写锁无热路径争用。内部 Arc 共享：守卫
/// 持注册表句柄，注销回落到**签发它的那张表**（局部表测试 / 全局表生产
/// 同一语义）。
#[derive(Clone)]
pub struct TaskActivityRegistry {
    tasks: std::sync::Arc<parking_lot::RwLock<BTreeMap<String, Arc<TaskActivity>>>>,
}

impl TaskActivityRegistry {
    /// 创建空注册表。
    pub fn new() -> Self {
        Self {
            tasks: std::sync::Arc::new(parking_lot::RwLock::new(BTreeMap::new())),
        }
    }

    /// 注册一个任务活动槽，返回注销守卫（drop = 自动向**本注册表**注销）。
    /// 同名已注册 → 换代覆盖（循环任务重启场景：旧代句柄仍可用，但 dump
    /// 只显示新代）。
    pub fn register(&self, name: &str) -> TaskGuard {
        let slot = Arc::new(TaskActivity::new(name));
        self.tasks.write().insert(name.to_string(), slot.clone());
        TaskGuard {
            registry: self.clone(),
            name: name.to_string(),
            slot,
        }
    }

    /// 移除任务（仅当当前注册的仍是传入槽——防旧代守卫误删新代条目）。
    fn remove_if_current(&self, name: &str, slot: &Arc<TaskActivity>) -> bool {
        let mut tasks = self.tasks.write();
        match tasks.get(name) {
            Some(current) if Arc::ptr_eq(current, slot) => tasks.remove(name).is_some(),
            _ => false,
        }
    }

    /// 全量快照（按任务名排序）。诊断读面，条目数 = 内核插桩任务数（百级内）。
    pub fn snapshot(&self) -> Vec<TaskDumpEntry> {
        self.tasks
            .read()
            .values()
            .map(|slot| TaskDumpEntry {
                name: slot.name().to_string(),
                label: slot.label(),
                age_secs: slot.age_secs(),
            })
            .collect()
    }

    /// 当前注册的任务数。
    pub fn len(&self) -> usize {
        self.tasks.read().len()
    }

    /// 是否为空。
    pub fn is_empty(&self) -> bool {
        self.tasks.read().is_empty()
    }
}

impl Default for TaskActivityRegistry {
    fn default() -> Self {
        Self::new()
    }
}

static GLOBAL_REGISTRY: OnceLock<TaskActivityRegistry> = OnceLock::new();

/// 进程级单例（与 PipelineStateRegistry 同款理由：不进 AppState，零体积增量）。
pub fn global_registry() -> &'static TaskActivityRegistry {
    GLOBAL_REGISTRY.get_or_init(TaskActivityRegistry::new)
}

/// RAII 注册守卫：drop 时把自己从签发注册表移除（仅当仍是自己——防旧代
/// 守卫误删新代条目）。
///
/// 长驻循环任务不 drop → 常驻在列；一次性任务跑完自动消失，dump 只列
/// 活着的任务。
pub struct TaskGuard {
    registry: TaskActivityRegistry,
    name: String,
    slot: Arc<TaskActivity>,
}

impl Drop for TaskGuard {
    fn drop(&mut self) {
        self.registry.remove_if_current(&self.name, &self.slot);
    }
}

impl std::ops::Deref for TaskGuard {
    type Target = TaskActivity;
    fn deref(&self) -> &TaskActivity {
        &self.slot
    }
}

/// 用任务守卫包住 future：体内 [`set_current_label`] 生效，跑完守卫自动注销。
///
/// 标签初值置 "running"；调用方持有守卫（不进 scope）= 长驻条目，由调用方
/// 决定注销时机。
pub fn scope<F>(guard: TaskGuard, fut: F) -> impl Future<Output = F::Output>
where
    F: Future,
{
    guard.set_label("running");
    CURRENT_SLOT.scope(Arc::clone(&guard.slot), async move {
        let _guard = guard;
        fut.await
    })
}

/// 带名派生一次性/循环任务：注册 → 标签 "running" → 跑完自动注销。
///
/// 循环任务体内应在既有等待点用 [`set_current_label`] 更新标签（如
/// "idle: next tick 6h" / "sweeping traces"）。
pub fn spawn_named<F>(name: &str, fut: F) -> tokio::task::JoinHandle<F::Output>
where
    F: Future + Send + 'static,
    F::Output: Send + 'static,
{
    let guard = global_registry().register(name);
    tokio::spawn(scope(guard, fut))
}

/// 在当前任务槽上更新活动标签（无槽 = no-op——非插桩任务零成本）。
///
/// 深层模块（engine/session 等）在既有等待点调用，不经参数穿槽。
pub fn set_current_label(label: impl AsRef<str>) {
    let _ = CURRENT_SLOT.try_with(|slot| slot.set_label(label));
}

/// 读当前任务槽的标签快照（无槽 = None）。诊断/测试用。
pub fn current_label() -> Option<String> {
    CURRENT_SLOT.try_with(|slot| slot.label()).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn register_snapshot_lists_name_label_age() {
        let reg = TaskActivityRegistry::new();
        let guard = reg.register("trace-retention");
        let entries = reg.snapshot();
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].name, "trace-retention");
        assert_eq!(
            entries[0].label, "starting",
            "注册初值；scope 接管后置 running"
        );
        assert_eq!(entries[0].age_secs, 0, "刚注册存活时长 < 1s，截断为 0");
        assert_eq!(reg.len(), 1);
        drop(guard);
        assert!(reg.is_empty(), "守卫 drop 必须向签发注册表注销条目");
    }

    #[test]
    fn set_label_visible_in_snapshot() {
        let reg = TaskActivityRegistry::new();
        let guard = reg.register("pipeline-run");
        guard.set_label("waiting human_interaction response req=abc-123");
        let entries = reg.snapshot();
        assert_eq!(
            entries[0].label,
            "waiting human_interaction response req=abc-123"
        );
        // 覆盖写：标签随等待点演进，只留最新。
        guard.set_label("idle");
        assert_eq!(reg.snapshot()[0].label, "idle");
    }

    #[test]
    fn same_name_reregister_replaces_and_old_guard_does_not_clobber() {
        let reg = TaskActivityRegistry::new();
        let old = reg.register("run-chain:p1");
        old.set_label("old-generation");
        let new = reg.register("run-chain:p1");
        assert_eq!(reg.len(), 1, "同名重注册 = 换代覆盖");
        let label = reg.snapshot()[0].label.clone();
        assert_eq!(
            label, "starting",
            "dump 显示新代初值（旧代 old-generation 不再可见）"
        );
        drop(new);
        assert!(reg.is_empty(), "新代守卫 drop 注销条目");
        drop(old);
        assert!(reg.is_empty(), "旧代守卫 drop 不得误删/复活条目");
    }

    #[test]
    fn set_current_label_noop_without_scope() {
        // 无槽上下文：不 panic、不产生条目。
        set_current_label("orphan");
        assert_eq!(current_label(), None);
    }

    #[tokio::test]
    async fn scope_propagates_label_and_unregisters_on_completion() {
        let reg = TaskActivityRegistry::new();
        let guard = reg.register("scope-test");
        let seen = {
            let fut = async {
                set_current_label("waiting admission gate");
                current_label().expect("scope 内标签可读")
            };
            scope(guard, fut).await
        };
        assert_eq!(seen, "waiting admission gate");
        assert!(reg.is_empty(), "scope 结束守卫自动注销");
    }

    #[tokio::test]
    async fn spawn_named_unregisters_after_completion() {
        let reg = global_registry();
        let handle = spawn_named("one-shot-task", async {
            set_current_label("working");
            42u32
        });
        assert_eq!(handle.await.expect("join"), 42);
        let entries: Vec<_> = reg
            .snapshot()
            .into_iter()
            .filter(|e| e.name == "one-shot-task")
            .collect();
        assert!(entries.is_empty(), "一次性任务跑完必须从 dump 消失");
    }
}
