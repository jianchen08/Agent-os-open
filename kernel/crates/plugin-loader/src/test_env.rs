//! 测试期用户空间环境钉桩（crate 级共享，plugin-loader 各测试模块通用）。
//!
//! 环境变量是进程全局态：用户空间相关用例互斥执行 + 自动还原。
//!
//! 可重入（线程内）：用例可能先显式取锁、再由 [`UserRootGuard`] 取一次——
//! 裸 `Mutex` 会自锁死。首个持有者记原始环境，最后一个释放时恢复。
//!
//! 同一 crate 内所有用户空间用例必须共用这一把锁：各测试模块自备私锁时互不
//! 互斥，跨模块仍会互相踩（enablement/loader 并行各设各的值）。

/// 环境变量是进程全局态：用户空间相关用例互斥执行 + 自动还原。
///
/// 可重入（线程内）：用例可能先显式取锁、再由 [`UserRootGuard`] 取一次——
/// 裸 `Mutex` 会自锁死。首个持有者记原始环境，最后一个释放时恢复。
pub(crate) fn user_space_env_lock() -> UserSpaceEnvGuard {
    static LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
    thread_local! {
        static DEPTH: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
    }
    let depth = DEPTH.with(|d| {
        let v = d.get();
        d.set(v + 1);
        v
    });
    if depth > 0 {
        return UserSpaceEnvGuard { _lock: None };
    }
    let lock = LOCK.lock().unwrap_or_else(|e| e.into_inner());
    UserSpaceEnvGuard {
        _lock: Option::Some(lock),
    }
}

/// [`user_space_env_lock`] 的 guard：最外层持有者 drop 时放锁。
pub(crate) struct UserSpaceEnvGuard {
    _lock: Option<std::sync::MutexGuard<'static, ()>>,
}

impl Drop for UserSpaceEnvGuard {
    fn drop(&mut self) {
        thread_local! {
            static DEPTH: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
        }
        DEPTH.with(|d| d.set(d.get().saturating_sub(1)));
    }
}

/// 把 `AGENTOS_USER_CONFIG_DIR` 钉到指定目录（Drop 还原），隔离真实用户空间。
///
/// 直接钉配置层而非 `USER_ROOT`：测试目录即配置层根，省去 `config/` 中转，
/// 且顺带覆盖「分区环境变量覆盖用户根」这条解析路径。
///
/// **必须经 [`user_space_env_lock`] 取锁**：环境变量是进程全局态，crate 内用例
/// 默认并行——只设不锁时，未钉桩的用例（如 load_config 空目录类）会读到别的
/// 用例刚设的值，或读到开发机真实用户目录里的残留配置（实测断言恒非空；
/// enablement 损坏档用例则相反，读到真实 profile 后解析成功、corrupted 恒 false）。
pub(crate) struct UserRootGuard {
    _lock: UserSpaceEnvGuard,
    original_cfg: Option<String>,
}

impl UserRootGuard {
    pub(crate) fn set(config_dir: &std::path::Path) -> Self {
        let lock = user_space_env_lock();
        let original_cfg = std::env::var(agentos_core::user_space::USER_CONFIG_DIR_ENV).ok();
        std::env::set_var(agentos_core::user_space::USER_CONFIG_DIR_ENV, config_dir);
        Self {
            _lock: lock,
            original_cfg,
        }
    }
}

impl Drop for UserRootGuard {
    fn drop(&mut self) {
        match &self.original_cfg {
            Some(v) => std::env::set_var(agentos_core::user_space::USER_CONFIG_DIR_ENV, v),
            None => std::env::remove_var(agentos_core::user_space::USER_CONFIG_DIR_ENV),
        }
    }
}
