// @feature: FP-0.2.一 插件协议 | @vision: V3 可嵌入 | @ci: rust-test
//! sidecar 进程崩溃（stdout EOF）时，进行中的 send_request 必须快速失败。
//!
//! 背景（工具调用"调用前卡死"根因之二）：reader loop 读到 sidecar stdout EOF
//! （进程退出/崩溃，如 `ModuleNotFoundError` import 失败）时，原实现只 warn +
//! break，**不 resolve pending**。进行中的 send_request（initialize / tools/call）
//! 只能等满 120s 超时（client.rs send_request 超时配置），用户感知为"工具调用
//! 在调用前卡死"。
//!
//! 复现要点：sidecar 必须先**阻塞读一行 stdin**（保证内核请求已成功写入管道），
//! 收到请求后再崩溃退出——这模拟真实场景"请求已发出、进程随后崩溃、响应永远
//! 不来"。若 sidecar 在请求发出前就退出，写入 stdin 会 EPIPE 快速失败，走不到
//! EOF 不 resolve pending 的卡死路径（初版测试即因此误通过）。
//!
//! 断言（用户可观察行为）：initialize 在 5s 内快速失败（Ok(Err)），而非超时
//! （Err）——后者等于复现 120s 卡死。
//!
//! [来源: docs/working/tool_call_hang_debug_report.md 根因2]

use std::time::Duration;

use agentos_mcp::client::McpClient;
use serde_json::json;

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

/// python 是否可用（不可用时跳过，避免 CI 无 python 环境失败）
fn python_available() -> bool {
    std::process::Command::new(python_exe())
        .arg("--version")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

#[tokio::test]
async fn test_send_request_fails_fast_on_sidecar_eof() {
    if !python_available() {
        eprintln!("python 不可用，跳过");
        return;
    }

    // sidecar 替身：阻塞读一行 stdin（保证内核请求写入成功），收到后崩溃退出。
    // 模拟真实场景：请求已发出、进程崩溃、响应永远不来。
    let script = "import sys; sys.stdin.readline(); sys.exit(1)";
    let mut client = McpClient::new_stdio(
        python_exe().to_string(),
        vec!["-c".to_string(), script.to_string()],
    );
    client.connect().await.expect("connect 应成功");

    // 等待 sidecar 就绪（阻塞在 readline）
    tokio::time::sleep(Duration::from_millis(500)).await;

    // initialize 应在 5s 内快速失败（Ok(Err)），而非超时（Err）——那等于 120s 卡死。
    let result = tokio::time::timeout(Duration::from_secs(5), client.initialize(&json!({}))).await;

    match result {
        Err(_) => {
            panic!("sidecar EOF 后 initialize 未快速失败：5s 内未返回（复现 120s 卡死）")
        }
        Ok(Err(_)) => {
            // 快速失败，符合预期：sidecar 崩溃应立刻报错，不阻塞调用方
        }
        Ok(Ok(_)) => {
            panic!("sidecar 已退出却 initialize 成功——异常路径");
        }
    }
}
