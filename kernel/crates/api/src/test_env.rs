//! 测试期用户空间隔离（`AGENTOS_USER_*` 环境变量钉桩）。
//!
//! 用户空间解析读进程环境（`agentos_core::user_space`），而环境变量是**进程全局
//! 态**——cargo test 默认多线程并行，一个用例 set、另一个读会互相踩。更严重的是
//! 不钉桩的用例会落到宿主机真实用户目录（Windows `%APPDATA%` 等），既不 hermetic
//! 又可能改写开发机的真实配置。
//!
//! 凡测试路径会经 `resolve_config_target` / `PluginEnablement::load` /
//! `resolve_pipeline_config_path` 的用例，一律先取 [`pin_user_root`]（或
//! [`pin_user_config_dir`]）再动作；guard drop 时恢复原值并释放锁。
//!
//! # 可重入
//!
//! 同一线程可能同时持有多个 guard（典型：脚手架函数里 pin 一次，用例再 pin 一次；
//! 或同一用例调用两次脚手架）。裸 `Mutex` 会让第二个 guard 自锁死，故这里用
//! 「进程级锁 + 线程内重入计数」：首个 guard 取锁并记快照，同线程后续 guard 只加
//! 计数（共享同一份快照），最后一个 drop 时才恢复环境并放锁。

use std::cell::RefCell;
use std::path::Path;
use std::sync::{Mutex, MutexGuard};

/// 跨线程互斥：环境变量是进程全局，任一线程钉桩期间其它线程不得进入。
static ENV_MUTEX: Mutex<()> = Mutex::new(());

thread_local! {
    /// 当前线程的重入态：guard 计数 + 首个 guard 记下的原始环境。
    ///
    /// 用 thread_local 而非全局共享态：重入只发生在同一线程内（嵌套调用），
    /// 跨线程由 [`ENV_MUTEX`] 串行化，二者职责正交。
    static REENTRANT: RefCell<Option<ReentrantState>> = const { RefCell::new(None) };
}

struct ReentrantState {
    /// 本线程持有的 guard 数：>0 表示环境已被本线程接管。
    depth: usize,
    /// 首次进入时保存的原始环境（最后一次 drop 时恢复）。
    saved: Vec<(&'static str, Option<String>)>,
}

/// 用户空间隔离 guard：持锁 + 钉桩环境变量，drop 时（最后一个）恢复。
pub(crate) struct UserSpaceGuard {
    /// 进程级锁的持有凭据（仅首个 guard 实际持有；其余为 None）。
    _lock: Option<MutexGuard<'static, ()>>,
}

impl Drop for UserSpaceGuard {
    fn drop(&mut self) {
        REENTRANT.with(|cell| {
            let mut slot = cell.borrow_mut();
            let Some(st) = slot.as_mut() else {
                return;
            };
            st.depth = st.depth.saturating_sub(1);
            if st.depth > 0 {
                return; // 本线程仍有其它 guard，环境继续由它接管
            }
            for (key, value) in st.saved.drain(..) {
                match value {
                    Some(v) => std::env::set_var(key, v),
                    None => std::env::remove_var(key),
                }
            }
            *slot = None;
            // 锁凭据随 `_lock` 字段（首个 guard）drop 自动释放
        });
    }
}

/// 钉桩用户空间根到 `root`，三个分区变量交给 `<root>` 的默认派生
/// （`config`/`data`/`plugins` 子目录）。
pub(crate) fn pin_user_root(root: &Path) -> UserSpaceGuard {
    pin(&[
        (agentos_core::user_space::USER_ROOT_ENV, Some(root)),
        (agentos_core::user_space::USER_CONFIG_DIR_ENV, None),
        (agentos_core::user_space::USER_DATA_DIR_ENV, None),
        (agentos_core::user_space::USER_PLUGINS_DIR_ENV, None),
    ])
}

/// 只钉桩用户配置层（其余分区清空，避免宿主机真实目录参与）。
pub(crate) fn pin_user_config_dir(dir: &Path) -> UserSpaceGuard {
    pin(&[
        (agentos_core::user_space::USER_ROOT_ENV, None),
        (agentos_core::user_space::USER_CONFIG_DIR_ENV, Some(dir)),
        (agentos_core::user_space::USER_DATA_DIR_ENV, None),
        (agentos_core::user_space::USER_PLUGINS_DIR_ENV, None),
    ])
}

/// 钉桩单个环境变量（进程全局态，与用户空间钉桩共用同一把锁与快照语义）。
///
/// `value = None` 清除该变量；同线程重复 pin 时后钉者赢，最后一个 guard drop
/// 时恢复首个 guard 记下的原文。
pub(crate) fn pin_env(key: &'static str, value: Option<&str>) -> UserSpaceGuard {
    pin(&[(key, value.map(Path::new))])
}

fn pin(vars: &[(&'static str, Option<&Path>)]) -> UserSpaceGuard {
    // 本线程已接管中 → 重定向到本次传入的目标（**后钉者赢**），只加计数。
    //
    // 语义选择：同一线程重复 pin 的典型场景是脚手架被调用多次（每次自带 tmp），
    // 调用方期望"最近一次钉的目标生效"；若沿用首次目标，第二次脚手架写出的文件
    // 会落到第一次的目录，用例里"写 tmp2、读 tmp2"的断言必然失败。
    // 快照仍只取首次进入时的原文，最后一次 drop 才恢复。
    let already_taken = REENTRANT.with(|cell| {
        let mut slot = cell.borrow_mut();
        match slot.as_mut() {
            Some(st) => {
                st.depth += 1;
                true
            }
            None => false,
        }
    });
    if already_taken {
        apply(vars);
        return UserSpaceGuard { _lock: None };
    }

    let lock_guard = ENV_MUTEX.lock().unwrap_or_else(|e| e.into_inner());
    let mut saved = Vec::with_capacity(vars.len());
    for (key, _value) in vars {
        saved.push((*key, std::env::var(key).ok()));
    }
    apply(vars);
    REENTRANT.with(|cell| {
        *cell.borrow_mut() = Some(ReentrantState { depth: 1, saved });
    });
    UserSpaceGuard {
        _lock: Some(lock_guard),
    }
}

/// 把一组变量按给定目标写入进程环境（None = 清除该变量）。
fn apply(vars: &[(&'static str, Option<&Path>)]) {
    for (key, value) in vars {
        match value {
            Some(p) => std::env::set_var(key, p),
            None => std::env::remove_var(key),
        }
    }
}
