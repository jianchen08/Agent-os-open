// @feature: FP-0.2.一 插件协议 | @vision: V3 可嵌入 | @ci: rust-test
//! 官方 mcp SDK（Python v2）sidecar 与 Rust McpClient 的真实对跑集成测试。
//!
//! Python 侧 server.py 的传输层已从自研 JSON-RPC 换为官方 `mcp` 包
//! （mcp.server.lowlevel.Server + stdio transport）。本测试以真实子进程 +
//! 真实 McpClient 验证私有协议扩展在官方 SDK 承载下完整可用：
//!
//! 1. initialize 握手：serverInfo/protocolVersion 协商 + capabilities/config 注入
//!    （sidecar 据此创建 CapabilityHandle）
//! 2. tools/call：分发层 schema 数值强转（LLM 字符串数值回归）
//! 3. sidecar→内核反向调用：工具 handler 内 cap.call → reader loop 路由到
//!    CapabilityRouter → 回写响应 → 工具结果携带内核返回
//! 4. sidecar→内核反向 notification：fire-and-forget 推送
//!
//! python / agentos_plugin_sdk 不可用时跳过（CI 环境防御）。

use std::sync::Mutex as StdMutex;

use async_trait::async_trait;
use serde_json::{json, Value};

use agentos_mcp::capability::CapabilityRouter;
use agentos_mcp::client::McpClient;
use agentos_mcp::error::McpError;

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

/// python + agentos_plugin_sdk（官方 mcp 2.0）是否可用。
fn sdk_sidecar_available() -> bool {
    let ok = std::process::Command::new(python_exe())
        .arg("-c")
        .arg("import agentos_plugin_sdk, mcp; import importlib.metadata as m; assert m.version('mcp').split('.')[0] == '2'")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    if !ok {
        eprintln!("python 或 agentos_plugin_sdk(mcp>=2) 不可用，跳过");
    }
    ok
}

/// 探针插件：echo（schema 强转验证）/ call_kernel（反向 request）/ notify_kernel（反向 notification）。
const SIDECAR_SCRIPT: &str = r#"
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("rust_e2e_probe")


@plugin.tool(
    name="echo",
    schema={
        "type": "object",
        "properties": {"text": {"type": "string"}, "num": {"type": "integer"}},
    },
    description="echo tool",
)
async def echo(text: str, num: int | None = None) -> dict:
    return {"echo": text, "num": num}


@plugin.tool(
    name="call_kernel",
    schema={"type": "object", "properties": {}},
    description="reverse capability request",
)
async def call_kernel() -> dict:
    cap = plugin.get_capability("pipeline-executor")
    result = await cap.call("resume", {"x": 1})
    return {"kernel_said": result}


@plugin.tool(
    name="notify_kernel",
    schema={"type": "object", "properties": {}},
    description="reverse capability notification",
)
async def notify_kernel() -> dict:
    cap = plugin.get_capability("event-bus")
    await cap.notify("emit", {"event": "stream_chunk", "chunk": "hi"})
    return {"notified": True}


if __name__ == "__main__":
    plugin.run()
"#;

/// 测试用 CapabilityRouter：记录调用并返回固定结果。
struct RecordingRouter {
    calls: StdMutex<Vec<(String, String, Value)>>,
}

#[async_trait]
impl CapabilityRouter for RecordingRouter {
    async fn handle(
        &self,
        capability: &str,
        method: &str,
        params: Value,
    ) -> Result<Value, McpError> {
        self.calls.lock().unwrap().push((
            capability.to_string(),
            method.to_string(),
            params.clone(),
        ));
        match (capability, method) {
            ("pipeline-executor", "resume") => Ok(json!({"status": "resumed_from_rust"})),
            ("event-bus", "emit") => Ok(json!({"emitted": true})),
            _ => Err(McpError::Protocol {
                message: format!("unknown capability method {}.{}", capability, method),
            }),
        }
    }

    fn known_namespaces(&self) -> Vec<String> {
        vec!["pipeline-executor".to_string(), "event-bus".to_string()]
    }
}

/// 解析 tools/call 结果 content[0].text 为 JSON（与内核 invoker 同路径）。
fn parse_tool_payload(result: &Value) -> Value {
    let text = result["content"][0]["text"]
        .as_str()
        .expect("content[0].text 应为字符串");
    serde_json::from_str(text).expect("content[0].text 应为 JSON 对象")
}

#[tokio::test]
async fn test_official_sdk_sidecar_full_roundtrip() {
    if !sdk_sidecar_available() {
        return;
    }

    let router = std::sync::Arc::new(RecordingRouter {
        calls: StdMutex::new(Vec::new()),
    });
    let mut client = McpClient::new_stdio(
        python_exe().to_string(),
        vec!["-c".to_string(), SIDECAR_SCRIPT.to_string()],
    )
    .with_router(router.clone());
    client.connect().await.expect("connect 应成功");

    // 1. initialize：官方 SDK 承载下握手应成功，serverInfo 为 SDK 身份
    let init = client
        .initialize(&json!({"model": "deepseek-chat", "retries": 2}))
        .await
        .expect("initialize 应成功");
    assert_eq!(init["serverInfo"]["name"], "agentos-plugin-sdk");
    assert_eq!(init["protocolVersion"], "2024-11-05");

    // 2. tools/list：schema 以 ToolDef 声明为准
    let tools = client.list_tools().await.expect("tools/list 应成功");
    let names: Vec<&str> = tools["tools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| t["name"].as_str().unwrap())
        .collect();
    assert!(names.contains(&"echo"), "tools/list 缺 echo: {names:?}");
    assert!(
        names.contains(&"call_kernel"),
        "tools/list 缺 call_kernel: {names:?}"
    );

    // 3. tools/call：字符串数值按 schema 强转（LLM 行为回归）
    let echo = client
        .call_tool("echo", &json!({"text": "hi", "num": "5"}))
        .await
        .expect("echo 调用应成功");
    let payload = parse_tool_payload(&echo);
    assert_eq!(payload["echo"], "hi");
    assert_eq!(payload["num"], 5, "字符串 '5' 应被强转为 int 5");

    // 4. 反向 capability 调用：工具 handler 内 cap.call → router 路由 → 回写
    let call_kernel = client
        .call_tool("call_kernel", &json!({}))
        .await
        .expect("call_kernel 调用应成功");
    let payload = parse_tool_payload(&call_kernel);
    assert_eq!(
        payload["kernel_said"]["status"], "resumed_from_rust",
        "反向调用应经 CapabilityRouter 返回固定结果: {payload}"
    );
    {
        let calls = router.calls.lock().unwrap();
        assert!(
            calls
                .iter()
                .any(|(c, m, _)| c == "pipeline-executor" && m == "resume"),
            "router 应收到 pipeline-executor.resume: {calls:?}"
        );
    }

    // 5. 反向 capability notification：fire-and-forget 推送
    let notify = client
        .call_tool("notify_kernel", &json!({}))
        .await
        .expect("notify_kernel 调用应成功");
    let payload = parse_tool_payload(&notify);
    assert_eq!(payload["notified"], true);

    // notification 是 spawn 异步处理，轮询等待 router 记录（最多 5s）
    for _ in 0..50 {
        {
            let calls = router.calls.lock().unwrap();
            if calls
                .iter()
                .any(|(c, m, p)| c == "event-bus" && m == "emit" && p["chunk"] == "hi")
            {
                break;
            }
        }
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    }
    {
        let calls = router.calls.lock().unwrap();
        assert!(
            calls
                .iter()
                .any(|(c, m, p)| c == "event-bus" && m == "emit" && p["chunk"] == "hi"),
            "router 应收到 event-bus.emit notification: {calls:?}"
        );
    }

    client.kill().await.expect("kill 应成功");
}

#[tokio::test]
async fn test_official_sdk_lifecycle_notification_reaches_sidecar() {
    if !sdk_sidecar_available() {
        return;
    }

    // 无 router 生命周期也能跑：notifications/on_load 是 fire-and-forget。
    // 注意：这里是普通 raw string（不走 format!），Python 字面量用单层花括号。
    let script = r#"
import json
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("lifecycle_probe")
state = {"got": None}


@plugin.on_load
async def _on_load(params: dict) -> None:
    state["got"] = params


@plugin.tool(
    name="last_on_load",
    schema={"type": "object", "properties": {}},
    description="return last on_load params",
)
async def last_on_load() -> dict:
    return {"got": state["got"]}


if __name__ == "__main__":
    plugin.run()
"#;

    let mut client = McpClient::new_stdio(
        python_exe().to_string(),
        vec!["-c".to_string(), script.to_string()],
    );
    client.connect().await.expect("connect 应成功");
    client
        .initialize(&json!({}))
        .await
        .expect("initialize 应成功");

    // 生命周期通知：任意 JSON params（config/tags）完整送达插件 handler
    client
        .send_notification(
            "notifications/on_load",
            Some(json!({"config": {"model": "deepseek"}, "tags": {"k": "v"}})),
        )
        .await
        .expect("on_load 通知应发送成功");

    // 通知处理与 tools/call 之间给 sidecar 一点调度时间（最多重试 10 次）
    for _ in 0..10 {
        let result = client
            .call_tool("last_on_load", &json!({}))
            .await
            .expect("last_on_load 调用应成功");
        let payload = parse_tool_payload(&result);
        if payload["got"].is_object() {
            assert_eq!(payload["got"]["config"]["model"], "deepseek");
            assert_eq!(payload["got"]["tags"]["k"], "v");
            client.kill().await.expect("kill 应成功");
            return;
        }
        tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    }
    panic!("on_load 通知未在 3s 内到达插件 handler");
}
