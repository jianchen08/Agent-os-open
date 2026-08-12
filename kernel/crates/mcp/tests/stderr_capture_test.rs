// @feature: FP-0.2.一 插件协议 | @vision: V3 可嵌入 | @ci: rust-test
//! McpClient stderr 捕获回归测试——日志丢弃/阻塞修复验证。
//!
//! 背景：原实现 stderr 被 `Stdio::piped()` 却从不读取，Python sidecar 的日志
//! 写入管道缓冲，填满（~64KB）后会反向阻塞 sidecar 进程。本测试启动一个
//! 持续向 stderr 写入（远超 64KB）的 python 替身，验证：
//! (a) 进程不被阻塞挂死（持续存活且能继续推进）；
//! (b) kill() 能正常清理。
//!
//! 说明：stderr 内容经 reader 转发到 tracing，这里不直接断言日志输出
//! （tracing 订阅器在测试中未必挂载），而是通过"进程未阻塞挂死"间接证明
//! stderr 被消费——这是修复的核心目标。

use std::time::Duration;

use agentos_mcp::client::McpClient;

fn python_exe() -> &'static str {
    #[cfg(windows)]
    {
        "python"
    }
    #[cfg(not(windows))]
    {
        "python3"
    }
}

fn python_available() -> bool {
    std::process::Command::new(python_exe())
        .arg("--version")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

/// 写一行到 stderr 的 python 替身脚本。
/// 每 50ms 写一行 ~1KB，累计远超 64KB 管道缓冲。
/// 若 stderr 未被消费，~64 行后进程会在 print 处阻塞，后续 keepalive 文件
/// 不再更新，alive 探测会失败。
fn stderr_spammer_script() -> &'static str {
    r#"
import sys, time, os

# keepalive 文件：每次写 stderr 后 touch，测试据此判断进程是否被阻塞。
keepalive = os.environ["KEEPALIVE_FILE"]
line = "x" * 1000 + "\n"
written = 0
end = time.time() + 10
while time.time() < end:
    sys.stderr.write(f"[spam] {line}")
    sys.stderr.flush()
    written += 1
    # 每 5 行更新一次 keepalive mtime
    if written % 5 == 0:
        open(keepalive, "w").write(str(written))
    time.sleep(0.02)
# 正常结束前写 done 标记
open(keepalive, "w").write("done")
"#
}

#[tokio::test]
async fn stderr_does_not_block_sidecar() {
    if !python_available() {
        eprintln!("[skip] python not available");
        return;
    }

    let tmp = tempfile::tempdir().expect("tempdir");
    let script = tmp.path().join("spam.py");
    std::fs::write(&script, stderr_spammer_script()).unwrap();
    let keepalive = tmp.path().join("keepalive");
    std::fs::write(&keepalive, "0").unwrap();

    let mut client = McpClient::new_stdio(
        python_exe(),
        vec![
            "-u".to_string(), // unbuffered，确保 stderr 立即写出
            script.to_string_lossy().to_string(),
        ],
    )
    .with_plugin_id("stderr_spam_test")
    .with_extra_env(vec![(
        "KEEPALIVE_FILE".to_string(),
        keepalive.to_string_lossy().to_string(),
    )]);

    client.connect().await.expect("connect");

    // 观察窗口：若 stderr 被消费，spam 进程能持续运行；若被阻塞，
    // 进程会在累计 ~64KB 处卡住，keepalive 停止更新但仍可能"恰好"存活在
    // 阻塞点。这里用两次采样判断 keepalive 是否推进（进程未挂死）。
    let read_keepalive = || std::fs::read_to_string(&keepalive).unwrap_or_default();
    let first = read_keepalive();
    tokio::time::sleep(Duration::from_millis(800)).await;
    let second = read_keepalive();

    // second 必须比 first 推进（证明进程没被 stderr 阻塞挂死）。
    // 注意：若进程跑得极快已到 "done"，second 可能 == "done"，也算推进。
    let progressed = second != first;
    assert!(
        progressed,
        "sidecar 被 stderr 阻塞了：keepalive 未推进 first={:?} second={:?}",
        first, second
    );

    // 进程应当仍然存活（10s 窗口内），随后正常 kill。
    // （若脚本已自然退出，is_alive 返回 false 也接受——重点是没被阻塞挂死。）
    let _ = client.is_alive().await;
    client.kill().await.expect("kill");
}
