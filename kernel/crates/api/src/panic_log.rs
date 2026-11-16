//! 内核 panic 痕迹落盘观测面。
//!
//! 现状契约：Rust panic 消息默认只写 stderr。装机 electron 已把 stderr 接线到
//! `kernel-stderr.log`（eacce1c35），但该 pipe 只在 electron 存活期间有效——
//! 2026-10-10 装机实证（B81 段）：旧内核进程跨装机文件替换存活为孤儿，其
//! stderr pipe 已无读者，12:56 崩溃时 panic/OOM 消息零痕迹（WER 只有
//! `0xc0000409`/P9=7 的 fail-fast 地址，无文本）。凡「内核活着而拉起方死了或
//! 未接线」的场景（孤儿、直接命令行拉起、服务化包装），stderr 面即失效。
//!
//! 本模块补一个与 stdio 接线状态无关的痕迹面：全局 panic hook 把 panic 消息、
//! 触发位置与 backtrace 追加写 `<log_dir>/kernel-panic.log`（单代轮转，与
//! electron 侧 `KERNEL_STDERR_MAX_BYTES` 同为 8MB 口径）。hook 采取链式：先落
//! 盘再调用前置 hook（`take_hook` 取回的默认 hook），默认 stderr 打印行为
//! 保持不变，eacce1c35 的 pipe 面不倒退。
//!
//! 边界：OOM（`rust_oom`）不经过 panic hook（独立处理器，stable 无
//! `set_alloc_error_hook`），本面覆盖不到——OOM 预警依赖 memstats 双阈值告警
//! （dedd1a3f3 P1），OOM 现场文本仍只能靠 stderr 管道。
//!
//! 写失败静默（观测面不反噬，与 electron 侧 appendKernelStderr 同纪律）。
// @feature: FP-0.2.可观测性 内核panic痕迹落盘 | @ci: rust-test

use std::io::{self, Write as _};
use std::panic;
use std::path::{Path, PathBuf};

/// 痕迹文件名（与 kernel.log.* 同目录，装机态在 `<USER_ROOT>/logs`）。
pub const PANIC_LOG_FILE: &str = "kernel-panic.log";

/// 单代轮转上限：与 electron `KERNEL_STDERR_MAX_BYTES`（8MB）同口径。
/// panic 痕迹低频，8MB 足够追溯；panic 循环风暴下防无界增长（磁盘治理面）。
pub const PANIC_LOG_MAX_BYTES: u64 = 8 * 1024 * 1024;

/// 追加一条报告到 `<log_dir>/kernel-panic.log`；文件超 `max_bytes` 时单代轮转
/// （current 改名 .old，新内容落新 current，与 electron 侧轮转语义一致）。
/// 返回 IO 错误供测试断言；生产调用方（hook）静默吞掉。
pub fn append_report(log_dir: &Path, report: &str, max_bytes: u64) -> io::Result<()> {
    std::fs::create_dir_all(log_dir)?;
    let current = log_dir.join(PANIC_LOG_FILE);
    let old = log_dir.join(format!("{PANIC_LOG_FILE}.old"));
    if let Ok(meta) = std::fs::metadata(&current) {
        if meta.len() >= max_bytes {
            // 旧 .old 直接覆盖（单代语义：只保最近一代完整痕迹）
            let _ = std::fs::remove_file(&old);
            std::fs::rename(&current, &old)?;
        }
    }
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&current)?;
    file.write_all(report.as_bytes())
}

/// panic payload 的文本化：&str / String 直取，其余归一为占位（Box<dyn Any> 无统一 Display）。
fn payload_text(info: &panic::PanicHookInfo<'_>) -> String {
    if let Some(s) = info.payload().downcast_ref::<&str>() {
        (*s).to_string()
    } else if let Some(s) = info.payload().downcast_ref::<String>() {
        s.clone()
    } else {
        "<非字符串 payload>".to_string()
    }
}

/// panic 触发位置的文本化（文件:行:列）。
fn location_text(info: &panic::PanicHookInfo<'_>) -> String {
    info.location()
        .map(|l| format!("{}:{}:{}", l.file(), l.line(), l.column()))
        .unwrap_or_else(|| "<未知位置>".to_string())
}

/// 格式化一条 panic 报告：时间戳（UTC RFC3339，与 kernel.log 纯 UTC 口径对齐）、
/// 线程名、payload、触发位置、backtrace。多行文本，单条自包含。
fn format_report(
    timestamp: &str,
    thread_name: &str,
    info: &panic::PanicHookInfo<'_>,
    backtrace: &str,
) -> String {
    format!(
        "\n[{timestamp}] thread <{thread_name}> panicked: {}\n  at {}\nbacktrace:\n{backtrace}\n",
        payload_text(info),
        location_text(info)
    )
}

/// 安装全局 panic hook（进程内只装一次）：先追加落盘（含单代轮转），再调用前置
/// hook——默认 hook（stderr 打印）经 `take_hook` 链式保留，stdio 接线面行为不变。
pub fn install(log_dir: &Path) {
    let previous = panic::take_hook();
    let dir: PathBuf = log_dir.to_path_buf();
    panic::set_hook(Box::new(move |info| {
        let thread_name = std::thread::current()
            .name()
            .unwrap_or("<unnamed>")
            .to_string();
        let report = format_report(
            &chrono::Utc::now().to_rfc3339(),
            &thread_name,
            info,
            &std::backtrace::Backtrace::force_capture().to_string(),
        );
        let _ = append_report(&dir, &report, PANIC_LOG_MAX_BYTES);
        tracing::error!(
            target: "agentos-kernel",
            "内核 panic（痕迹已落 {PANIC_LOG_FILE}）：{} @ {}",
            payload_text(info),
            location_text(info)
        );
        previous(info);
    }));
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    /// hook 是进程级全局：本模块 hook 类测试互斥串行，防并行测试互相换 hook。
    static HOOK_TESTS_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn append_creates_missing_dirs_and_appends() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path().join("a").join("b");
        append_report(&dir, "first\n", u64::MAX).unwrap();
        append_report(&dir, "second\n", u64::MAX).unwrap();
        let content = std::fs::read_to_string(dir.join(PANIC_LOG_FILE)).unwrap();
        assert_eq!(content, "first\nsecond\n", "追加语义：两条按序拼接");
        assert!(
            !dir.join(format!("{PANIC_LOG_FILE}.old")).exists(),
            "未超限不轮转"
        );
    }

    #[test]
    fn append_rotates_single_generation_when_over_limit() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path();
        append_report(dir, "x".repeat(20).as_str(), 16).unwrap();
        append_report(dir, "y".repeat(5).as_str(), 16).unwrap();
        let old = std::fs::read_to_string(dir.join(format!("{PANIC_LOG_FILE}.old"))).unwrap();
        let current = std::fs::read_to_string(dir.join(PANIC_LOG_FILE)).unwrap();
        assert_eq!(old, "x".repeat(20), "超限时整代让位到 .old");
        assert_eq!(current, "y".repeat(5), "新内容从空 current 起步");
        // 时序：current=5B < 阈值，再写 20B 不轮转（current 变 25B）；下一笔才触发
        append_report(dir, "z".repeat(20).as_str(), 16).unwrap();
        assert_eq!(
            std::fs::read_to_string(dir.join(PANIC_LOG_FILE)).unwrap(),
            "y".repeat(5) + &"z".repeat(20),
            "未超限继续追加不轮转"
        );
        // 再次超限：.old 被覆盖为最近一代（25B），不与第一代（20B）累积
        append_report(dir, "w", 16).unwrap();
        assert_eq!(
            std::fs::read_to_string(dir.join(format!("{PANIC_LOG_FILE}.old"))).unwrap(),
            "y".repeat(5) + &"z".repeat(20),
            "单代轮转：.old 只保最近一代"
        );
        assert_eq!(
            std::fs::read_to_string(dir.join(PANIC_LOG_FILE)).unwrap(),
            "w",
            "轮转后新 current 只含本轮内容"
        );
    }

    #[test]
    fn install_writes_trace_and_chains_previous_hook() {
        let _guard = HOOK_TESTS_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let tmp = tempfile::tempdir().unwrap();
        let default_hook = panic::take_hook();
        // 前置 hook 用标记文件留痕，链式调用必须触发它（stderr 面不倒退的等价断言）
        let marker = tmp.path().join("prev_called.marker");
        let marker_for_hook = marker.clone();
        panic::set_hook(Box::new(move |_| {
            let _ = std::fs::write(&marker_for_hook, b"1");
        }));
        install(tmp.path());
        let result = std::panic::catch_unwind(|| panic!("boom-{}", 123));
        // 先恢复默认再断言：断言自身的 panic 不再流经被测链（报错必须可见）
        panic::set_hook(default_hook);
        assert!(result.is_err(), "panic 应照常 unwind");
        assert_eq!(
            std::fs::read(marker).unwrap(),
            b"1",
            "前置 hook 必须被链式调用（stderr 面不倒退）"
        );
        let content = std::fs::read_to_string(tmp.path().join(PANIC_LOG_FILE)).unwrap();
        assert!(
            content.contains("panicked: boom-123"),
            "payload 必须在痕迹中，实际: {content}"
        );
        assert!(
            // 位置含源文件名与行列号（分隔符平台相关，只断文件名锚点）
            content.contains("panic_log.rs:") && content.contains("  at "),
            "触发位置必须指向本模块源码，实际: {content}"
        );
        assert!(content.contains("backtrace:"), "backtrace 段必须存在");
        assert!(
            content.starts_with('\n') && content.contains('[') && content.contains('+'),
            "首行应为 [UTC RFC3339] 时间戳格式，实际首行: {:?}",
            content.lines().next()
        );
    }

    #[test]
    fn install_two_panics_land_in_order() {
        let _guard = HOOK_TESTS_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let tmp = tempfile::tempdir().unwrap();
        let previous = panic::take_hook();
        install(tmp.path());
        let _ = std::panic::catch_unwind(|| panic!("first-α"));
        let _ = std::panic::catch_unwind(|| panic!("second-β"));
        panic::set_hook(previous);
        let content = std::fs::read_to_string(tmp.path().join(PANIC_LOG_FILE)).unwrap();
        let first = content.find("panicked: first-α").expect("第一条在案");
        let second = content.find("panicked: second-β").expect("第二条在案");
        assert!(first < second, "两条痕迹必须按发生顺序追加");
    }
}
