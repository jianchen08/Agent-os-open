// @feature: FP-0.2.一 插件协议 | @vision: V3 可嵌入 | @ci: rust-test
//! stderr 非 UTF-8 字节回归测试——sidecar 阻塞（120s 超时）根因验证。
//!
//! 背景（llm_core.execute 120s 超时根因）：Windows 宿主上 Python sidecar 的
//! stderr 输出（如 traceback 含 GBK 编码中文路径）可能含非 UTF-8 字节。原实现
//! 用 `BufRead::read_line`（严格 UTF-8 解码），遇到非法字节返回 `InvalidData`
//! 错误 → break 退出 stderr 消费循环 → sidecar 继续写 stderr，管道缓冲（64KB）
//! 填满后 `write` 阻塞 → sidecar 进程卡死 → 无法响应 MCP 请求 → 内核 120s 超时
//! （用户感知"工具调用卡死"）。
//!
//! 本测试用一个 Python 替身 sidecar：
//! 1. 先向 stderr 写非 UTF-8 字节（触发原实现 reader break）；
//! 2. 再向 stderr 写远超 64KB 管道缓冲的内容（若 reader 已 break，这里会阻塞）；
//! 3. 最后响应 MCP initialize 请求。
//!
//! 断言（用户可观察行为）：initialize 在 5s 内成功返回（Ok(Ok)），而非 5s 超时
//! （Err）——后者等于复现"sidecar 被 stderr 阻塞 → 120s 卡死"。
//!
//! [来源: docs/working/llm_core_timeout_fix_report.md]

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

/// sidecar 替身脚本：
/// - 先写非 UTF-8 字节到 stderr（触发 reader InvalidData）
/// - 再写 ~200KB 到 stderr（>64KB 管道缓冲，若 reader 已 break 则这里阻塞）
/// - 最后进入 stdin 循环响应 MCP initialize
fn sidecar_script() -> &'static str {
    r#"
import sys, json

# 1) 非 UTF-8 字节（0xff 0xfe 0xfd 不是合法 UTF-8）
sys.stderr.buffer.write(b"non-utf8: \xff\xfe\xfd\n")
sys.stderr.flush()

# 2) 大量 stderr：200 行 x ~1KB = ~200KB，远超 64KB 管道缓冲。
#    若内核 stderr reader 已因非 UTF-8 break，此处 write 会阻塞在缓冲填满，
#    永远走不到第 3 步 → initialize 超时。
line = b"z" * 1000 + b"\n"
for _ in range(200):
    sys.stderr.buffer.write(line)
    sys.stderr.flush()

# 3) 响应 MCP initialize（stdin 读一行 JSON-RPC）
for raw in sys.stdin.buffer:
    msg = json.loads(raw.decode("utf-8", errors="replace"))
    if msg.get("method") == "initialize":
        resp = {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {"ok": True, "serverInfo": {"name": "t", "version": "0"}},
        }
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()
        break
"#
}

#[tokio::test]
async fn stderr_invalid_utf8_does_not_block_sidecar() {
    if !python_available() {
        eprintln!("python 不可用，跳过");
        return;
    }

    let mut client = McpClient::new_stdio(
        python_exe().to_string(),
        vec![
            "-u".to_string(),
            "-c".to_string(),
            sidecar_script().to_string(),
        ],
    )
    .with_plugin_id("stderr_invalid_utf8_test");

    client.connect().await.expect("connect 应成功");

    // initialize 应在 5s 内成功返回。
    // 修复前：stderr reader 遇非 UTF-8 break → sidecar 写 ~64KB 后阻塞 →
    // initialize 永远等不到响应 → 5s 超时（Err）→ 测试失败。
    // 修复后：stderr reader 用 lossy 解码继续消费 → sidecar 写完 200KB →
    // 进入 stdin 循环响应 initialize → Ok(Ok) → 测试通过。
    let result = tokio::time::timeout(Duration::from_secs(5), client.initialize(&json!({}))).await;

    match result {
        Err(_) => {
            panic!("sidecar 被 stderr 非 UTF-8 阻塞：initialize 5s 内未返回（复现 120s 卡死根因）")
        }
        Ok(Err(e)) => {
            panic!("initialize 快速失败（非预期）：{}", e)
        }
        Ok(Ok(_)) => {
            // 符合预期：非 UTF-8 stderr 不应阻塞 sidecar，initialize 应正常完成
        }
    }

    client.kill().await.expect("kill");
}
