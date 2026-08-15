# @feature: FP-0.2.一 第三方插件协议 | @vision: V3 可嵌入 | @ci: python-plugins-test
"""官方 mcp SDK v2 承载的 MCP 服务端——与 Rust 内核协议兼容性端到端测试。

测试进程扮演内核 McpClient（kernel/crates/mcp/src/client.rs 的逐字镜像）：
- 字符串 JSON-RPC id（uuid hex）
- initialize 附送私有 capabilities/config（依赖注入）
- notifications/initialized 带 ``"params": null``（内核 unwrap_or(Value::Null) 行为）
- 生命周期通知 ``notifications/<hook>`` 推送任意 JSON params
- sidecar 反向调用 ``<capability>.<method>``：识别 method+id → 路由回写 response；
  method 无 id → fire-and-forget notification

sidecar 以真实子进程经 stdin/stdout 拉起（官方 stdio transport，Windows 线程池 IO），
覆盖完整 wire 格式（换行分隔 JSON），非内存流模拟。
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import uuid
from typing import Any

import pytest

# sidecar 插件脚本：echo / 反向调用 / 反向通知 / 生命周期回读 四类工具
SIDECAR_SCRIPT = """
import sys
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("e2e_probe")
_last_lifecycle: dict[str, Any] = {}


@plugin.on_load
async def _on_load(params: dict) -> None:
    _last_lifecycle["on_load"] = params


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    _last_lifecycle["on_unload"] = params


@plugin.tool(
    name="echo",
    schema={
        "type": "object",
        "properties": {"text": {"type": "string"}, "num": {"type": "integer"}},
    },
    description="Echo tool",
)
async def echo(text: str, num: int | None = None) -> dict:
    return {"echo": text, "num": num}


@plugin.tool(
    name="call_kernel",
    schema={"type": "object", "properties": {}},
    description="Reverse capability call",
)
async def call_kernel() -> dict:
    cap = plugin.get_capability("pipeline-executor")
    result = await cap.call("resume", {"x": 1})
    return {"kernel_said": result}


@plugin.tool(
    name="notify_kernel",
    schema={"type": "object", "properties": {}},
    description="Reverse capability notification",
)
async def notify_kernel() -> dict:
    cap = plugin.get_capability("event-bus")
    await cap.notify("emit", {"event": "stream_chunk", "chunk": "hello"})
    return {"notified": True}


@plugin.tool(
    name="last_lifecycle",
    schema={"type": "object", "properties": {}},
    description="Return lifecycle payloads received so far",
)
async def last_lifecycle() -> dict:
    return dict(_last_lifecycle)


if __name__ == "__main__":
    plugin.run()
"""


class KernelHarness:
    """逐字模拟内核 McpClient 的最小 JSON-RPC 对端。

    - 读循环线程逐行消费 stdout；response 按 id 关联 pending future
    - sidecar 反向 request（method+id）→ 回写 result（模拟 CapabilityRouter）
    - sidecar 反向 notification（method 无 id）→ 记录到 received_notifications
    """

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc
        self._pending: dict[str, queue.Queue[dict]] = {}
        self.received_notifications: list[dict[str, Any]] = []
        self.received_requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        # 反向调用模拟：capability.method → 内核响应 result
        self.reverse_handlers: dict[str, Any] = {
            "pipeline-executor.resume": {"status": "resumed", "echo_x": None},
        }

        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_reader.start()

    def _read_stdout(self) -> None:
        """读循环（镜像内核 reader loop）：逐行解析并在线程内直接分发。

        - response（无 method 有 id）→ 关联 pending queue
        - sidecar 反向 request（method + id）→ 记录并回写 result
        - sidecar 反向 notification（method 无 id）→ 记录
        """
        assert self._proc.stdout is not None
        for raw in self._proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            if msg.get("method") is None and "id" in msg:
                with self._lock:
                    q = self._pending.pop(str(msg["id"]), None)
                if q is not None:
                    q.put(msg)
                continue

            if msg.get("method") is not None and "id" in msg:
                self.received_requests.append(msg)
                result = self.reverse_handlers.get(msg["method"], {"routed": True, "method": msg["method"]})
                self._write({"jsonrpc": "2.0", "id": msg["id"], "result": result})
                continue

            if msg.get("method") is not None:
                self.received_notifications.append(msg)

    def _read_stderr(self) -> None:
        # 消费 stderr 防管道阻塞（sidecar 日志走 stderr）；保留最近行供失败诊断
        assert self._proc.stderr is not None
        for _ in self._proc.stderr:
            pass

    def _drain_one(self, timeout: float) -> dict[str, Any] | None:
        """已废弃的兼容占位（分发在读线程内完成），保留给 wait_notification 轮询节奏。"""
        import time

        time.sleep(min(timeout, 0.2))
        return None

    def _write(self, obj: dict[str, Any]) -> None:
        assert self._proc.stdin is not None
        with self._write_lock:
            self._proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            self._proc.stdin.flush()

    def request(self, method: str, params: Any = None, timeout: float = 30.0) -> dict[str, Any]:
        """发送 JSON-RPC request（字符串 id，镜像内核），等待并返回 result。"""
        req_id = uuid.uuid4().hex
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        q: queue.Queue[dict] = queue.Queue()
        with self._lock:
            self._pending[req_id] = q
        self._write(payload)

        while True:
            try:
                msg = q.get(timeout=timeout)
            except queue.Empty:
                raise AssertionError(f"timeout waiting response for {method} (id={req_id})") from None
            if "error" in msg and msg["error"] is not None:
                raise AssertionError(f"{method} returned error: {msg['error']}")
            return msg.get("result") or {}

    def notify(self, method: str, params: Any = None) -> None:
        """发送 notification（无 id）。params=None 时写 "params": null（镜像内核）。"""
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        self._write(payload)

    def wait_notification(self, method: str, timeout: float = 15.0) -> dict[str, Any]:
        """等待 sidecar 发出的指定 method 反向 notification。"""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for n in self.received_notifications:
                if n.get("method") == method:
                    return n
            self._drain_one(0.2)
        raise AssertionError(f"timeout waiting notification {method}")

    def stop(self) -> None:
        try:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        except Exception:
            pass


@pytest.fixture()
def harness(tmp_path: Any) -> KernelHarness:
    script = tmp_path / "e2e_probe_sidecar.py"
    script.write_text(SIDECAR_SCRIPT, encoding="utf-8")
    proc = subprocess.Popen(  # noqa: S603 - 探针脚本内容为测试内常量，非外部输入
        [sys.executable, str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(tmp_path),
    )
    h = KernelHarness(proc)
    yield h
    h.stop()


@pytest.fixture()
def initialized(harness: KernelHarness) -> KernelHarness:
    """完成 initialize 握手（镜像内核 initialize + notifications/initialized）。"""
    result = harness.request(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "pipeline-executor": {},
                "config-reader": {},
                "tenant-context": {},
                "event-bus": {},
                "logger": {},
                "metrics": {},
            },
            "clientInfo": {"name": "agentos", "version": "0.2.0"},
            "config": {"model": "deepseek-chat", "retries": 2},
        },
    )
    assert result["serverInfo"]["name"] == "agentos-plugin-sdk"
    # 内核发送 initialized 通知（params 显式 null）
    harness.notify("notifications/initialized", None)
    return harness


def _call_tool(h: KernelHarness, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = h.request("tools/call", {"name": name, "arguments": arguments})
    assert result.get("isError") is False, result
    return json.loads(result["content"][0]["text"])


class TestOfficialSdkE2E:
    def test_initialize_handshake_and_di(self, initialized: KernelHarness) -> None:
        """initialize 握手成功：serverInfo 正确、协议版本协商回显内核请求值。"""
        result = initialized.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "agentos", "version": "0.2.0"},
                "config": {},
            },
        )
        assert result["protocolVersion"] == "2024-11-05"
        assert result["serverInfo"]["name"] == "agentos-plugin-sdk"

    def test_tools_list_preserves_declared_schema(self, initialized: KernelHarness) -> None:
        """tools/list：工具名/description/inputSchema 以 ToolDef 声明为准。"""
        result = initialized.request("tools/list")
        tools = {t["name"]: t for t in result["tools"]}
        assert "echo" in tools and "call_kernel" in tools
        assert tools["echo"]["description"] == "Echo tool"
        assert tools["echo"]["inputSchema"]["properties"]["num"]["type"] == "integer"

    def test_tools_call_echo_with_numeric_coercion(self, initialized: KernelHarness) -> None:
        """tools/call：字符串数值参数按 schema 强转（LLM 行为回归）。"""
        body = _call_tool(initialized, "echo", {"text": "hi", "num": "5"})
        assert body == {"echo": "hi", "num": 5}

    def test_reverse_capability_call_roundtrip(self, initialized: KernelHarness) -> None:
        """工具 handler 内反向调用内核 capability：request 路由 + response 回写。"""
        body = _call_tool(initialized, "call_kernel", {})
        assert body["kernel_said"]["status"] == "resumed"
        # 内核侧确实收到了反向 request，method 为 <capability>.<method>
        assert any(r.get("method") == "pipeline-executor.resume" for r in initialized.received_requests)

    def test_reverse_capability_notification(self, initialized: KernelHarness) -> None:
        """工具 handler 内反向 notification（流式 chunk 推送路径）。"""
        body = _call_tool(initialized, "notify_kernel", {})
        assert body == {"notified": True}
        n = initialized.wait_notification("event-bus.emit")
        assert n["params"]["chunk"] == "hello"

    def test_lifecycle_notification_on_load(self, initialized: KernelHarness) -> None:
        """notifications/on_load：任意 JSON params 完整送达插件生命周期 handler。"""
        initialized.notify("notifications/on_load", {"config": {"model": "deepseek"}, "tags": {"k": "v"}})
        body = _call_tool(initialized, "last_lifecycle", {})
        assert body["on_load"] == {"config": {"model": "deepseek"}, "tags": {"k": "v"}}

    def test_unknown_lifecycle_hook_ignored(self, initialized: KernelHarness) -> None:
        """未知 hook 通知安全忽略，不崩溃、不断连接（后续调用仍正常）。"""
        initialized.notify("notifications/on_some_future_hook", {"anything": 1})
        body = _call_tool(initialized, "echo", {"text": "still-alive"})
        assert body["echo"] == "still-alive"

    def test_unknown_tool_returns_jsonrpc_error(self, initialized: KernelHarness) -> None:
        """未知工具 → JSON-RPC error 响应（内核包装为 Protocol 错误）。"""
        req_id = uuid.uuid4().hex
        q: queue.Queue[dict] = queue.Queue()
        with initialized._lock:
            initialized._pending[req_id] = q
        initialized._write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {"name": "no_such_tool", "arguments": {}},
            }
        )
        msg = q.get(timeout=30)
        assert msg.get("error") is not None
        assert "tool not found" in msg["error"]["message"]
