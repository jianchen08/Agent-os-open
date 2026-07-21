"""
用户旅程 3：WebSocket 通信验证

使用 websocket-client 库验证 Kernel 的 WebSocket 端点。
代码参考: kernel/crates/api/src/server.rs 中的 handle_ws_connection

测试项:
  3.1 连接 ws://localhost:9100/ws，收到欢迎消息
      {"type":"connected","content":"...","session_id":"...","timestamp":"..."}
  3.2 发送消息 {"message":"hello e2e","session_id":"test-session"}，
      收到 echo 响应 {"type":"message","content":"Echo: hello e2e",...}
  3.3 验证 session_id 透传正确
"""
import json
import pytest

try:
    import websocket
    HAS_WEBSOCKET = True
except ImportError:
    HAS_WEBSOCKET = False


def _skip_if_no_websocket():
    """如果没有 websocket-client 库，跳过测试。"""
    if not HAS_WEBSOCKET:
        pytest.skip("websocket-client 未安装，跳过 WebSocket 测试")


def _connect_ws(ws_url, timeout=5):
    """创建 WebSocket 连接，返回连接对象。"""
    return websocket.create_connection(ws_url, timeout=timeout)


class TestWebSocketConnection:
    """3.1 WebSocket 连接和欢迎消息。"""

    def test_ws_connect_receives_welcome_message(self, ws_url):
        """测试: 连接 /ws 后应收到欢迎消息。"""
        _skip_if_no_websocket()
        ws = _connect_ws(ws_url)
        try:
            welcome_raw = ws.recv()
            welcome = json.loads(welcome_raw)
            assert welcome.get("type") == "connected", \
                f"欢迎消息 type 期望 'connected'，实际 '{welcome.get('type')}'"
        finally:
            ws.close()

    def test_ws_welcome_has_content_field(self, ws_url):
        """测试: 欢迎消息包含 content 字段。"""
        _skip_if_no_websocket()
        ws = _connect_ws(ws_url)
        try:
            welcome = json.loads(ws.recv())
            assert "content" in welcome, "欢迎消息缺少 content 字段"
            assert isinstance(welcome["content"], str), "content 应为字符串"
            assert len(welcome["content"]) > 0, "content 不应为空"
        finally:
            ws.close()

    def test_ws_welcome_has_session_id_field(self, ws_url):
        """测试: 欢迎消息包含 session_id 字段（服务端生成）。"""
        _skip_if_no_websocket()
        ws = _connect_ws(ws_url)
        try:
            welcome = json.loads(ws.recv())
            assert "session_id" in welcome, "欢迎消息缺少 session_id 字段"
            assert isinstance(welcome["session_id"], str), "session_id 应为字符串"
            assert len(welcome["session_id"]) > 0, "session_id 不应为空"
        finally:
            ws.close()

    def test_ws_welcome_has_timestamp_field(self, ws_url):
        """测试: 欢迎消息包含 timestamp 字段。"""
        _skip_if_no_websocket()
        ws = _connect_ws(ws_url)
        try:
            welcome = json.loads(ws.recv())
            assert "timestamp" in welcome, "欢迎消息缺少 timestamp 字段"
            assert isinstance(welcome["timestamp"], str), "timestamp 应为字符串"
        finally:
            ws.close()


class TestWebSocketEcho:
    """3.2 WebSocket 消息收发（echo 响应）。"""

    def test_ws_send_message_receives_echo(self, ws_url):
        """测试: 发送消息后应收到 echo 响应。"""
        _skip_if_no_websocket()
        ws = _connect_ws(ws_url)
        try:
            # 先消费欢迎消息
            ws.recv()

            # 发送消息
            ws.send(json.dumps({
                "message": "hello e2e",
                "session_id": "test-session",
            }))

            # 接收 echo 响应
            response_raw = ws.recv()
            response = json.loads(response_raw)
            assert response.get("type") == "message", \
                f"响应 type 期望 'message'，实际 '{response.get('type')}'"
        finally:
            ws.close()

    def test_ws_echo_content_contains_message(self, ws_url):
        """测试: echo 响应的 content 包含发送的消息文本。"""
        _skip_if_no_websocket()
        ws = _connect_ws(ws_url)
        try:
            ws.recv()  # 欢迎消息

            ws.send(json.dumps({
                "message": "hello e2e",
                "session_id": "test-session",
            }))
            response = json.loads(ws.recv())
            assert "content" in response, "响应缺少 content 字段"
            assert "hello e2e" in response["content"], \
                f"content 期望包含 'hello e2e'，实际 '{response.get('content')}'"
        finally:
            ws.close()

    def test_ws_echo_has_timestamp_field(self, ws_url):
        """测试: echo 响应包含 timestamp 字段。"""
        _skip_if_no_websocket()
        ws = _connect_ws(ws_url)
        try:
            ws.recv()

            ws.send(json.dumps({
                "message": "hello e2e",
                "session_id": "test-session",
            }))
            response = json.loads(ws.recv())
            assert "timestamp" in response, "响应缺少 timestamp 字段"
            assert isinstance(response["timestamp"], str), "timestamp 应为字符串"
        finally:
            ws.close()


class TestWebSocketSessionPassthrough:
    """3.3 session_id 透传验证。"""

    def test_ws_session_id_passthrough(self, ws_url):
        """测试: 发送的 session_id 应在 echo 响应中原样返回。"""
        _skip_if_no_websocket()
        ws = _connect_ws(ws_url)
        try:
            ws.recv()

            test_session_id = "test-session-passthrough-12345"
            ws.send(json.dumps({
                "message": "session test",
                "session_id": test_session_id,
            }))
            response = json.loads(ws.recv())
            assert response.get("session_id") == test_session_id, \
                f"session_id 透传错误：期望 '{test_session_id}'，实际 '{response.get('session_id')}'"
        finally:
            ws.close()

    def test_ws_empty_session_id_passthrough(self, ws_url):
        """测试: 发送空 session_id 时，响应中 session_id 也为空。"""
        _skip_if_no_websocket()
        ws = _connect_ws(ws_url)
        try:
            ws.recv()

            ws.send(json.dumps({
                "message": "empty session test",
                "session_id": "",
            }))
            response = json.loads(ws.recv())
            assert response.get("session_id") == "", \
                f"空 session_id 透传错误：期望空字符串，实际 '{response.get('session_id')}'"
        finally:
            ws.close()

    def test_ws_custom_session_id_passthrough(self, ws_url):
        """测试: 发送自定义 session_id，验证透传正确性。"""
        _skip_if_no_websocket()
        ws = _connect_ws(ws_url)
        try:
            ws.recv()

            custom_id = "my-custom-session-id-abc-def-ghi"
            ws.send(json.dumps({
                "message": "custom session",
                "session_id": custom_id,
            }))
            response = json.loads(ws.recv())
            assert response.get("session_id") == custom_id, \
                f"自定义 session_id 透传错误：期望 '{custom_id}'，实际 '{response.get('session_id')}'"
        finally:
            ws.close()
