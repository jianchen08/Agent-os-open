// @feature: FP-0.2.一 插件协议 | @vision: V3 可嵌入 | @ci: rust-test
//! McpClient.kill() 整树杀测试——治理缺口回归：
//!
//! 原实现只 kill 直接子进程（sidecar 本体），bash 工具拉起的孙进程会变
//! 孤儿。本测试验证 kill() 后孙进程一并终止（Windows taskkill /T /F、
//! Unix 进程组信号两条路径）。
//!
//! 测试方法：用 McpClient 启动一个 python "sidecar 替身"，替身再拉起一个
//! sleep 300s 的孙进程并把其 pid 写入临时文件；kill() 后轮询断言孙进程消失。

use std::fs;
use std::time::Duration;

use tempfile::tempdir;

use agentos_mcp::client::McpClient;

#[cfg(unix)]
extern crate libc;

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

/// 检测进程是否存活（Windows tasklist / Unix kill -0）。
async fn is_process_alive(pid: u32) -> bool {
    #[cfg(windows)]
    {
        let output = std::process::Command::new("tasklist")
            .args(["/FI", &format!("PID eq {pid}"), "/NH"])
            .output();
        match output {
            Ok(out) => {
                let text = String::from_utf8_lossy(&out.stdout);
                // 存活时输出含 PID 行；不存在时输出 "INFO: No tasks"
                text.contains(&pid.to_string())
            }
            Err(_) => true, // tasklist 不可用，保守视为存活
        }
    }
    #[cfg(unix)]
    {
        unsafe { libc::kill(pid as i32, 0) == 0 }
    }
}

/// 轮询等待 pid 文件出现，返回孙进程 pid
async fn wait_for_grandchild_pid(pid_file: &std::path::Path) -> Option<u32> {
    for _ in 0..50 {
        if let Ok(content) = fs::read_to_string(pid_file) {
            if let Ok(pid) = content.trim().parse::<u32>() {
                return Some(pid);
            }
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
    None
}

/// 轮询等待进程消失（最多 10s）
async fn wait_for_process_death(pid: u32) -> bool {
    for _ in 0..50 {
        if !is_process_alive(pid).await {
            return true;
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
    false
}

#[tokio::test]
async fn kill_terminates_whole_process_tree() {
    if !python_available() {
        eprintln!("python 不可用，跳过 kill 树测试");
        return;
    }

    let dir = tempdir().expect("tempdir");
    let pid_file = dir.path().join("grandchild.pid");
    let pid_file_str = pid_file.to_str().expect("utf8 path").to_string();

    // sidecar 替身：拉起 sleep 300s 的孙进程，写 pid 到文件，然后自己 sleep
    let script = format!(
        "import subprocess, sys, time\n\
         g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n\
         open({pid_file:?}, 'w').write(str(g.pid))\n\
         time.sleep(300)\n",
        pid_file = pid_file_str,
    );

    let mut client = McpClient::new_stdio(python_exe(), vec!["-c".into(), script]);
    client.connect().await.expect("connect sidecar stand-in");

    let grandchild_pid = wait_for_grandchild_pid(&pid_file)
        .await
        .expect("grandchild pid should be reported");
    eprintln!("grandchild pid = {grandchild_pid}");

    // 杀掉 sidecar（整树杀：直接子进程 + 孙进程应一并消失）
    client.kill().await.expect("kill should succeed");

    let gone = wait_for_process_death(grandchild_pid).await;
    assert!(
        gone,
        "孙进程 {grandchild_pid} 在 sidecar kill 后仍存活（整树杀未生效）"
    );
}
