"""灵汐 AgentOS 0.2 Rust 内核 API 功能验证测试。

验证内核二进制的以下功能旅程：
1. 健康检查：GET /health 返回 200，body 含 status=ok, version=0.2.0
2. Schema 聚合：GET /api/v1/schema 返回 200，body 含 agents/pipelines/tools/routes 字段
3. 消息收发(REST)：POST /api/v1/chat 发送消息，返回 200，content 包含响应
4. 消息收发(WebSocket)：连接 ws://host:port/ws，发送消息 JSON，收到 Echo 响应
5. 各能力清单端点：GET /api/v1/agents, /pipelines, /tools 全部返回 200

[来源: kernel/crates/api/src/server.rs — 路由树定义]
[来源: kernel/crates/api/src/routes.rs — 端点处理器]
[来源: kernel/crates/api/src/bin/agentos-kernel.rs — 内核入口]
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# ============================================================================
# 常量
# ============================================================================

KERNEL_PORT = int(os.environ.get("AGENTOS_KERNEL_TEST_PORT", "9100"))
KERNEL_HOST = "127.0.0.1"
BASE_URL = f"http://{KERNEL_HOST}:{KERNEL_PORT}"

# 内核二进制路径（相对于项目根 /workspace）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
KERNEL_BIN = _PROJECT_ROOT / "kernel" / "target" / "release" / "agentos-kernel"

# 等待内核就绪的超时（秒）
STARTUP_TIMEOUT = 30
STARTUP_POLL_INTERVAL = 0.5


# ============================================================================
# HTTP 工具函数（纯标准库，零依赖）
# ============================================================================


def http_get(path: str, timeout: float = 10.0) -> tuple[int, dict]:
    """发送 GET 请求，返回 (status_code, json_body)。

    Args:
        path: URL 路径，如 /health
        timeout: 请求超时秒数

    Returns:
        (HTTP 状态码, 解析后的 JSON dict)

    Raises:
        urllib.error.URLError: 连接失败
        json.JSONDecodeError: 响应体非 JSON
    """
    url = f"{BASE_URL}{path}"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        return resp.status, json.loads(body)


def http_post(path: str, payload: dict, timeout: float = 10.0) -> tuple[int, dict]:
    """发送 POST 请求，返回 (status_code, json_body)。

    Args:
        path: URL 路径
        payload: 请求体 JSON 数据
        timeout: 请求超时秒数

    Returns:
        (HTTP 状态码, 解析后的 JSON dict)
    """
    url = f"{BASE_URL}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        return resp.status, json.loads(body)


# ============================================================================
# WebSocket 客户端（纯标准库实现 RFC 6455）
# ============================================================================


class WebSocketClient:
    """最小化 WebSocket 客户端，基于 Python 标准库实现。

    支持：
    - WebSocket 握手（Sec-WebSocket-Key/Accept）
    - 发送/接收文本帧
    - 接收 Pong 帧（自动处理）
    """

    def __init__(self, host: str, port: int, path: str = "/ws", timeout: float = 10.0):
        self._host = host
        self._port = port
        self._path = path
        self._timeout = timeout
        self._sock: socket.socket | None = None

    def connect(self) -> None:
        """建立 WebSocket 连接。"""
        # 1. TCP 连接
        self._sock = socket.create_connection(
            (self._host, self._port), timeout=self._timeout
        )

        # 2. WebSocket 握手
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET {self._path} HTTP/1.1\r\n"
            f"Host: {self._host}:{self._port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._sock.sendall(handshake.encode("ascii"))

        # 3. 读取握手响应
        response = self._recv_http_headers()
        if "101" not in response.split("\r\n")[0]:
            raise ConnectionError(f"WebSocket 握手失败: {response[:200]}")

        # 验证 Sec-WebSocket-Accept
        expected_accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if expected_accept not in response:
            raise ConnectionError("WebSocket 握手响应缺少有效的 Sec-WebSocket-Accept")

    def _recv_http_headers(self) -> str:
        """逐字节读取直到 \\r\\n\\r\\n，返回 HTTP 响应头。"""
        assert self._sock is not None
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self._sock.recv(1)
            if not chunk:
                break
            data += chunk
        return data.decode("ascii")

    def send_text(self, message: str) -> None:
        """发送 WebSocket 文本帧（客户端帧必须带掩码）。"""
        assert self._sock is not None
        payload = message.encode("utf-8")
        mask_key = os.urandom(4)

        # 构造帧头
        frame = bytearray()
        frame.append(0x81)  # FIN=1, opcode=0x1 (text)

        # Payload length + MASK=1
        length = len(payload)
        if length <= 125:
            frame.append(0x80 | length)
        elif length <= 65535:
            frame.append(0x80 | 126)
            frame.extend(struct.pack(">H", length))
        else:
            frame.append(0x80 | 127)
            frame.extend(struct.pack(">Q", length))

        # Masking key
        frame.extend(mask_key)

        # Masked payload
        masked = bytearray(len(payload))
        for i, byte in enumerate(payload):
            masked[i] = byte ^ mask_key[i % 4]
        frame.extend(masked)

        self._sock.sendall(bytes(frame))

    def recv_text(self) -> str:
        """接收一个 WebSocket 文本帧，返回 payload 字符串。

        自动处理 Ping 帧（回复 Pong）和连续帧。
        """
        assert self._sock is not None

        while True:
            # 读取帧头前 2 字节
            header = self._recv_exact(2)
            opcode = header[0] & 0x0F
            masked = (header[1] >> 7) & 1
            length = header[1] & 0x7F

            # 扩展长度
            if length == 126:
                ext = self._recv_exact(2)
                length = struct.unpack(">H", ext)[0]
            elif length == 127:
                ext = self._recv_exact(8)
                length = struct.unpack(">Q", ext)[0]

            # 掩码键（服务器通常不掩码，但兼容处理）
            mask_key = b""
            if masked:
                mask_key = self._recv_exact(4)

            # Payload
            payload = self._recv_exact(length)
            if masked:
                payload = bytes(payload[i] ^ mask_key[i % 4] for i in range(len(payload)))

            # 处理控制帧
            if opcode == 0x8:  # Close
                raise ConnectionError("服务器关闭了 WebSocket 连接")
            if opcode == 0x9:  # Ping → 回复 Pong
                self._send_pong(payload)
                continue
            if opcode == 0xA:  # Pong
                continue

            # 文本帧
            if opcode == 0x1 or opcode == 0x0:
                return payload.decode("utf-8")

    def _recv_exact(self, n: int) -> bytes:
        """精确读取 n 个字节。"""
        assert self._sock is not None
        data = bytearray()
        while len(data) < n:
            chunk = self._sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError(f"连接断开，已读取 {len(data)}/{n} 字节")
            data.extend(chunk)
        return bytes(data)

    def _send_pong(self, payload: bytes) -> None:
        assert self._sock is not None
        frame = bytearray()
        frame.append(0x8A)  # FIN=1, opcode=0xA (pong)
        frame.append(len(payload))
        frame.extend(payload)
        self._sock.sendall(bytes(frame))

    def close(self) -> None:
        """关闭 WebSocket 连接。"""
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


# ============================================================================
# 内核进程管理 fixture
# ============================================================================


def _build_kernel_if_needed() -> Path:
    """检查内核二进制是否存在，不存在则尝试编译。

    Returns:
        内核二进制路径
    """
    if KERNEL_BIN.exists() and os.access(KERNEL_BIN, os.X_OK):
        return KERNEL_BIN

    # 尝试编译
    cargo_toml = _PROJECT_ROOT / "kernel" / "Cargo.toml"
    if not cargo_toml.exists():
        pytest.skip(f"内核项目不存在: {cargo_toml}")

    print(f"[setup] 内核二进制不存在，尝试编译: {KERNEL_BIN}")
    result = subprocess.run(
        ["cargo", "build", "--release", "--bin", "agentos-kernel"],
        cwd=str(_PROJECT_ROOT / "kernel"),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        pytest.fail(
            f"内核编译失败 (exit={result.returncode}):\n"
            f"stdout: {result.stdout[-2000:]}\n"
            f"stderr: {result.stderr[-2000:]}"
        )

    assert KERNEL_BIN.exists(), f"编译完成但二进制仍不存在: {KERNEL_BIN}"
    return KERNEL_BIN


def _start_kernel() -> subprocess.Popen:
    """启动内核进程，返回 Popen 对象。

    使用 AGENTOS_KERNEL_PORT 环境变量指定端口。
    """
    binary = _build_kernel_if_needed()

    env = os.environ.copy()
    env["AGENTOS_KERNEL_PORT"] = str(KERNEL_PORT)
    env["AGENTOS_KERNEL_HOST"] = KERNEL_HOST

    proc = subprocess.Popen(
        [str(binary)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # 在 Linux 上创建新进程组，便于 kill 整个进程树
        start_new_session=True,
    )

    # 等待健康检查通过
    deadline = time.time() + STARTUP_TIMEOUT
    last_error = ""
    while time.time() < deadline:
        # 检查进程是否意外退出
        if proc.poll() is not None:
            output = ""
            if proc.stdout:
                output = proc.stdout.read().decode("utf-8", errors="replace")
            pytest.fail(
                f"内核进程意外退出 (exit={proc.returncode}):\n{output[-3000:]}"
            )

        try:
            status, body = http_get("/health", timeout=2.0)
            if status == 200:
                print(f"[setup] 内核就绪 (port={KERNEL_PORT}), health={body}")
                return proc
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            last_error = str(e)

        time.sleep(STARTUP_POLL_INTERVAL)

    # 超时，清理并失败
    _stop_kernel(proc)
    pytest.fail(
        f"内核在 {STARTUP_TIMEOUT}s 内未就绪 (port={KERNEL_PORT}), 最后错误: {last_error}"
    )


def _stop_kernel(proc: subprocess.Popen) -> None:
    """优雅关闭内核进程。"""
    if proc.poll() is not None:
        return  # 进程已退出

    try:
        # 发送 SIGTERM 给整个进程组
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass

    # 等待 5 秒优雅退出
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        # 强制 kill
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        proc.wait(timeout=3)

    print("[teardown] 内核进程已关闭")


@pytest.fixture(scope="module")
def kernel_process() -> subprocess.Popen:
    """模块级 fixture：启动内核，所有测试共享同一内核实例。

    Yields:
        内核进程 Popen 对象
    """
    proc = _start_kernel()
    yield proc
    _stop_kernel(proc)


@pytest.fixture(scope="module")
def base_url() -> str:
    """返回内核基础 URL。"""
    return BASE_URL


# ============================================================================
# 测试：1. 健康检查
# ============================================================================


class TestHealthCheck:
    """健康检查端点 GET /health 验证。"""

    def test_health_returns_200(self, kernel_process):
        """测试: GET /health 返回 HTTP 200。

        意图: 确认内核服务已启动且可响应请求。
        [来源: kernel/crates/api/src/server.rs L51 — /health 路由]
        """
        status, body = http_get("/health")
        assert status == 200, f"/health 返回非 200 状态码: {status}, body={body}"

    def test_health_body_contains_status_ok(self, kernel_process):
        """测试: /health 响应 body 中 status 字段为 "ok"。

        意图: 健康检查必须明确报告服务状态为正常。
        [来源: kernel/crates/api/src/routes.rs L53 — health_handler 返回 status="ok"]
        """
        status, body = http_get("/health")
        assert status == 200
        assert body.get("status") == "ok", f"status 字段非 ok: {body.get('status')}"

    def test_health_body_contains_version(self, kernel_process):
        """测试: /health 响应 body 中 version 字段为 "0.2.0"。

        意图: 确认运行的是正确的内核版本。
        [来源: kernel/Cargo.toml L17 — version = "0.2.0"]
        """
        status, body = http_get("/health")
        assert status == 200
        assert body.get("version") == "0.2.0", (
            f"version 非 0.2.0: {body.get('version')}"
        )

    def test_health_body_contains_timestamp(self, kernel_process):
        """测试: /health 响应 body 中包含 timestamp 字段。

        意图: 时间戳表明这是实时响应而非缓存。
        [来源: kernel/crates/api/src/routes.rs L56 — timestamp: chrono::Utc::now()]
        """
        status, body = http_get("/health")
        assert status == 200
        assert "timestamp" in body, f"缺少 timestamp 字段: {body}"
        assert isinstance(body["timestamp"], str)
        assert len(body["timestamp"]) > 0


# ============================================================================
# 测试：2. Schema 聚合
# ============================================================================


class TestSchemaAggregation:
    """Schema 聚合端点 GET /api/v1/schema 验证。"""

    def test_schema_returns_200(self, kernel_process):
        """测试: GET /api/v1/schema 返回 HTTP 200。

        意图: Schema 端点正常可用。
        [来源: kernel/crates/api/src/server.rs L53 — /api/v1/schema 路由]
        """
        status, body = http_get("/api/v1/schema")
        assert status == 200, f"/api/v1/schema 返回非 200: {status}"

    def test_schema_contains_agents_field(self, kernel_process):
        """测试: schema 响应包含 agents 字段。

        意图: 前端依赖此字段渲染 Agent 清单。
        [来源: kernel/crates/api/src/routes.rs L23 — agents: Vec]
        """
        status, body = http_get("/api/v1/schema")
        assert status == 200
        assert "agents" in body, f"缺少 agents 字段: {list(body.keys())}"
        assert isinstance(body["agents"], list)

    def test_schema_contains_pipelines_field(self, kernel_process):
        """测试: schema 响应包含 pipelines 字段。

        意图: 前端依赖此字段渲染 Pipeline 清单。
        [来源: kernel/crates/api/src/routes.rs L24 — pipelines: Vec]
        """
        status, body = http_get("/api/v1/schema")
        assert status == 200
        assert "pipelines" in body, f"缺少 pipelines 字段: {list(body.keys())}"
        assert isinstance(body["pipelines"], list)

    def test_schema_contains_tools_field(self, kernel_process):
        """测试: schema 响应包含 tools 字段。

        意图: 前端依赖此字段渲染工具清单。
        [来源: kernel/crates/api/src/routes.rs L25 — tools: Vec]
        """
        status, body = http_get("/api/v1/schema")
        assert status == 200
        assert "tools" in body, f"缺少 tools 字段: {list(body.keys())}"
        assert isinstance(body["tools"], list)

    def test_schema_contains_routes_field(self, kernel_process):
        """测试: schema 响应包含 routes 字段。

        意图: routes 定义插件路由拓扑，是 Schema 聚合的核心。
        [来源: kernel/crates/api/src/routes.rs L26 — routes: serde_json::Value]
        """
        status, body = http_get("/api/v1/schema")
        assert status == 200
        assert "routes" in body, f"缺少 routes 字段: {list(body.keys())}"


# ============================================================================
# 测试：3. 消息收发 (REST)
# ============================================================================


class TestChatREST:
    """消息发送端点 POST /api/v1/chat 验证。"""

    def test_chat_returns_200(self, kernel_process):
        """测试: POST /api/v1/chat 返回 HTTP 200。

        意图: REST fallback 消息端点正常可用。
        [来源: kernel/crates/api/src/server.rs L60 — /api/v1/chat 路由]
        """
        status, body = http_post("/api/v1/chat", {"message": "hello", "session_id": "test-1"})
        assert status == 200, f"POST /api/v1/chat 返回非 200: {status}"

    def test_chat_response_has_type_message(self, kernel_process):
        """测试: chat 响应 type 字段为 "message"。

        意图: 响应格式符合 WebSocket 统一消息协议。
        [来源: kernel/crates/api/src/server.rs L130 — type: "message"]
        """
        status, body = http_post("/api/v1/chat", {"message": "hello", "session_id": "s1"})
        assert status == 200
        assert body.get("type") == "message", f"type 非 message: {body.get('type')}"

    def test_chat_response_contains_echoed_content(self, kernel_process):
        """测试: chat 响应 content 包含发送的消息内容。

        意图: 确认消息被正确处理并返回（而非空响应）。
        [来源: kernel/crates/api/src/server.rs L131 — content: "Response to: {message}"]
        """
        test_message = "你好世界hello_world_42"
        status, body = http_post("/api/v1/chat", {"message": test_message, "session_id": "s1"})
        assert status == 200
        content = body.get("content", "")
        assert test_message in content, (
            f"响应 content 不包含原始消息: content='{content}', expected='{test_message}'"
        )

    def test_chat_response_preserves_session_id(self, kernel_process):
        """测试: chat 响应正确回传 session_id。

        意图: session_id 是会话上下文追踪的关键，必须正确传递。
        [来源: kernel/crates/api/src/server.rs L132 — session_id: req.session_id]
        """
        session_id = "session-abc-123-xyz"
        status, body = http_post(
            "/api/v1/chat", {"message": "test", "session_id": session_id}
        )
        assert status == 200
        assert body.get("session_id") == session_id, (
            f"session_id 不匹配: got='{body.get('session_id')}', expected='{session_id}'"
        )

    def test_chat_response_has_timestamp(self, kernel_process):
        """测试: chat 响应包含 timestamp 字段。

        意图: 时间戳用于消息排序和审计追踪。
        [来源: kernel/crates/api/src/server.rs L133 — timestamp: chrono::Utc::now()]
        """
        status, body = http_post("/api/v1/chat", {"message": "test", "session_id": "s1"})
        assert status == 200
        assert "timestamp" in body, f"缺少 timestamp 字段: {body}"
        assert isinstance(body["timestamp"], str)


# ============================================================================
# 测试：4. 消息收发 (WebSocket)
# ============================================================================


class TestChatWebSocket:
    """WebSocket 端点 WS /ws 消息收发验证。"""

    def test_ws_can_connect(self, kernel_process):
        """测试: 能成功建立 WebSocket 连接。

        意图: WebSocket 端点可连接，这是实时通信的基础。
        [来源: kernel/crates/api/src/server.rs L58 — /ws 路由]
        """
        ws = WebSocketClient(KERNEL_HOST, KERNEL_PORT, "/ws")
        try:
            ws.connect()
        finally:
            ws.close()

    def test_ws_receives_connected_welcome(self, kernel_process):
        """测试: 连接后收到 type=connected 的欢迎消息。

        意图: 确认 WebSocket 握手成功后服务端主动推送连接确认。
        [来源: kernel/crates/api/src/server.rs L74-81 — 发送欢迎消息]
        """
        ws = WebSocketClient(KERNEL_HOST, KERNEL_PORT, "/ws")
        try:
            ws.connect()
            msg = ws.recv_text()
            data = json.loads(msg)
            assert data.get("type") == "connected", (
                f"欢迎消息 type 非 connected: {data.get('type')}"
            )
            assert "content" in data, f"欢迎消息缺少 content: {data}"
            assert "session_id" in data, f"欢迎消息缺少 session_id: {data}"
            assert "timestamp" in data, f"欢迎消息缺少 timestamp: {data}"
        finally:
            ws.close()

    def test_ws_echo_response(self, kernel_process):
        """测试: 发送消息后收到 type=message 的 Echo 响应。

        意图: 验证完整 WebSocket 双向通信——客户端发消息，服务端正确 echo。
        [来源: kernel/crates/api/src/server.rs L96-101 — Echo 响应]
        """
        test_message = "ws_test_message_789"
        ws = WebSocketClient(KERNEL_HOST, KERNEL_PORT, "/ws")
        try:
            ws.connect()

            # 先消费欢迎消息
            welcome = ws.recv_text()
            welcome_data = json.loads(welcome)
            assert welcome_data["type"] == "connected"

            # 发送测试消息
            ws.send_text(json.dumps({"message": test_message, "session_id": "ws-session-1"}))

            # 接收 Echo 响应
            echo = ws.recv_text()
            echo_data = json.loads(echo)

            assert echo_data["type"] == "message", (
                f"Echo 响应 type 非 message: {echo_data.get('type')}"
            )
            assert test_message in echo_data.get("content", ""), (
                f"Echo content 不包含原始消息: content='{echo_data.get('content')}'"
            )
            assert echo_data.get("session_id") == "ws-session-1", (
                f"Echo session_id 不匹配: got='{echo_data.get('session_id')}'"
            )
            assert "timestamp" in echo_data, f"Echo 响应缺少 timestamp: {echo_data}"
        finally:
            ws.close()

    def test_ws_multiple_rounds(self, kernel_process):
        """测试: WebSocket 连续发送多条消息，每条都能收到正确响应。

        意图: 验证 WebSocket 连接是持久的，支持多轮对话。
        """
        ws = WebSocketClient(KERNEL_HOST, KERNEL_PORT, "/ws")
        try:
            ws.connect()
            ws.recv_text()  # 消费欢迎消息

            for i in range(3):
                msg_text = f"round_{i}_message"
                ws.send_text(json.dumps({"message": msg_text, "session_id": f"multi-{i}"}))
                resp = ws.recv_text()
                resp_data = json.loads(resp)
                assert resp_data["type"] == "message"
                assert msg_text in resp_data["content"]
                assert resp_data["session_id"] == f"multi-{i}"
        finally:
            ws.close()


# ============================================================================
# 测试：5. 各能力清单端点
# ============================================================================


class TestCapabilityListEndpoints:
    """能力清单端点验证: /api/v1/agents, /pipelines, /tools。"""

    def test_agents_returns_200(self, kernel_process):
        """测试: GET /api/v1/agents 返回 HTTP 200。

        意图: Agent 清单端点正常可用。
        [来源: kernel/crates/api/src/server.rs L54 — /api/v1/agents 路由]
        """
        status, body = http_get("/api/v1/agents")
        assert status == 200, f"/api/v1/agents 返回非 200: {status}"

    def test_agents_returns_list(self, kernel_process):
        """测试: /api/v1/agents 返回数组结构。

        意图: Agent 清单必须是 JSON 数组，供前端遍历渲染。
        [来源: kernel/crates/api/src/routes.rs L96 — Vec<serde_json::Value>]
        """
        status, body = http_get("/api/v1/agents")
        assert status == 200
        assert isinstance(body, list), f"agents 响应非数组: {type(body)}"

    def test_pipelines_returns_200(self, kernel_process):
        """测试: GET /api/v1/pipelines 返回 HTTP 200。

        意图: Pipeline 清单端点正常可用。
        [来源: kernel/crates/api/src/server.rs L55 — /api/v1/pipelines 路由]
        """
        status, body = http_get("/api/v1/pipelines")
        assert status == 200, f"/api/v1/pipelines 返回非 200: {status}"

    def test_pipelines_returns_list(self, kernel_process):
        """测试: /api/v1/pipelines 返回数组结构。

        意图: Pipeline 清单必须是 JSON 数组。
        [来源: kernel/crates/api/src/routes.rs L109 — Vec<serde_json::Value>]
        """
        status, body = http_get("/api/v1/pipelines")
        assert status == 200
        assert isinstance(body, list), f"pipelines 响应非数组: {type(body)}"

    def test_tools_returns_200(self, kernel_process):
        """测试: GET /api/v1/tools 返回 HTTP 200。

        意图: 工具清单端点正常可用。
        [来源: kernel/crates/api/src/server.rs L56 — /api/v1/tools 路由]
        """
        status, body = http_get("/api/v1/tools")
        assert status == 200, f"/api/v1/tools 返回非 200: {status}"

    def test_tools_returns_list(self, kernel_process):
        """测试: /api/v1/tools 返回数组结构。

        意图: 工具清单必须是 JSON 数组。
        [来源: kernel/crates/api/src/routes.rs L122 — Vec<serde_json::Value>]
        """
        status, body = http_get("/api/v1/tools")
        assert status == 200
        assert isinstance(body, list), f"tools 响应非数组: {type(body)}"
