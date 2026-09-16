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

/// 测试期日志采集缓冲（[`capture_logs`] 的返回物）。
///
/// 视图语义 = 「自本 handle 创建以来追加的日志」：底层是进程级共享缓冲（见
/// [`capture_logs`]），本 handle 只截取自己创建之后的增量。
pub(crate) struct LogBuffer {
    sink: std::sync::Arc<Mutex<Vec<u8>>>,
    start: usize,
}

impl LogBuffer {
    /// 自本 handle 创建以来写入的日志全文。
    pub(crate) fn text(&self) -> String {
        let buf = self.sink.lock().unwrap();
        let from = self.start.min(buf.len());
        String::from_utf8_lossy(&buf[from..]).into_owned()
    }
}

#[derive(Clone)]
struct LogWriter(std::sync::Arc<Mutex<Vec<u8>>>);

impl std::io::Write for LogWriter {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        self.0.lock().unwrap().extend_from_slice(buf);
        Ok(buf.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

impl<'a> tracing_subscriber::fmt::MakeWriter<'a> for LogWriter {
    type Writer = LogWriter;
    fn make_writer(&'a self) -> Self::Writer {
        self.clone()
    }
}

/// 进程级采集缓冲（首次 [`capture_logs`] 时随全局订阅一起装配）。
static CAPTURE_SINK: std::sync::OnceLock<std::sync::Arc<Mutex<Vec<u8>>>> =
    std::sync::OnceLock::new();

/// [`capture_logs`] 的 guard：全局订阅为进程级、不可逐用例卸载，guard 只是
/// 保持与既有调用点的接口一致的占位（drop 后新日志仍会进共享缓冲，但本
/// handle 的 `text()` 只回放自创建以来的增量，不因此串味）。
pub(crate) struct LogGuard {
    _priv: (),
}

/// 装配进程级日志采集订阅（TRACE 级——诊断留痕断言含 debug! 文案，采集面
/// 不得低于被断言的最低级别）并返回「自此刻起」的采集视图。
///
/// tracing 宏的消息体只在存在订阅者时才求值——无订阅者时诊断日志行恒零命中。
///
/// 必须是**进程级**订阅而非线程级 `set_default`：调用点兴趣缓存是进程级的，
/// 线程级订阅在并行测试下会让调用点因他线程重算缓存而翻成"无兴趣"短路
/// （表现为同一用例时绿时红）。全局订阅一次装上后所有线程恒有兴趣，各用例
/// 经 [`LogBuffer`] 的起始偏移各取各的增量。
///
/// 并发用例的日志会互相出现在同一缓冲里——断言须用本用例独有的文案或标识
/// （不得依赖"缓冲里只有我这一条"）。
pub(crate) fn capture_logs() -> (LogGuard, LogBuffer) {
    let sink = std::sync::Arc::clone(CAPTURE_SINK.get_or_init(|| {
        let sink = std::sync::Arc::new(Mutex::new(Vec::new()));
        let subscriber = tracing_subscriber::fmt()
            .with_ansi(false)
            .with_max_level(tracing::Level::TRACE)
            .with_writer(LogWriter(std::sync::Arc::clone(&sink)))
            .finish();
        // 已设全局订阅（他处 set_global_default）时保持安静：采集视图仍可用，
        // 只是可能收不到日志——由断言失败暴露，不静默伪装成"没有留痕"。
        let _ = tracing::subscriber::set_global_default(subscriber);
        tracing::callsite::rebuild_interest_cache();
        sink
    }));
    let start = {
        let buf = sink.lock().unwrap();
        buf.len()
    };
    (LogGuard { _priv: () }, LogBuffer { sink, start })
}
