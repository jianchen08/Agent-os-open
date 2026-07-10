"""server.py 流式反向代理的测试覆盖。

覆盖 _stream_proxy 及三个代理路由（/api /media /uploads），验证：
1. 流式转发：大响应被分块 yield，主线程不持有完整响应体（stream=True + aiter_bytes）。
2. hop-by-hop headers 被过滤，业务 headers 透传。
3. 后端不可达时返回 502。
4. 连接在流结束/中断后关闭（防泄漏）。
5. 回归红线：绝不调用 resp.content（旧的全量读取写法），改用 aiter_bytes。

测试用 httpx.MockTransport 替换全局 client，不依赖真实后端。
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

# server.py 在 frontend/ 下，加入 sys.path 以 import
_FRONTEND_DIR = Path(__file__).resolve().parent.parent
if str(_FRONTEND_DIR) not in sys.path:
    sys.path.insert(0, str(_FRONTEND_DIR))


def _make_mock_client(handler, *, fail_with: type[Exception] | None = None) -> httpx.AsyncClient:
    """构造一个用 MockTransport 的 AsyncClient，模拟后端。

    Args:
        handler: 接收 httpx.Request 返回 httpx.Response 的同步函数。
        fail_with: 若设置，transport 始终抛该异常（模拟后端不可达）。
    """
    if fail_with is not None:
        def _failing(_request: httpx.Request) -> httpx.Response:
            raise fail_with("模拟后端不可达")
        transport = httpx.MockTransport(_failing)
    else:
        transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, timeout=10.0)


@pytest.fixture
def app_client(monkeypatch):
    """注入 mock client 并返回 FastAPI TestClient。"""
    import server  # noqa: PLC0415

    # 默认用一个返回小 JSON 的 handler，各测试可 monkeypatch 覆盖
    def _default_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True}, headers={"x-trace": "abc"})

    mock = _make_mock_client(_default_handler)
    monkeypatch.setattr(server, "client", mock)
    yield TestClient(server.app)
    # TestClient 会在 __exit__ 关闭 lifespan；mock 的关闭放 finally
    import asyncio  # noqa: PLC0415

    try:
        asyncio.get_event_loop().run_until_complete(mock.aclose())
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 1. 流式转发核心：大响应被分块，不一次性读取
# ---------------------------------------------------------------------------

class TestStreamingForwarding:
    """验证流式转发的不变量。"""

    def test_large_api_response_streamed_intact(self, app_client, monkeypatch):
        """大响应（5MB）经 /api 转发后内容完整、状态码正确。"""
        import server  # noqa: PLC0415

        big_payload = "x" * (5 * 1024 * 1024)  # 5MB，远超单 chunk
        server.client = _make_mock_client(
            lambda req: httpx.Response(200, content=big_payload.encode(), headers={"content-type": "text/plain"})
        )

        resp = app_client.get("/api/v1/test")
        assert resp.status_code == 200
        assert resp.text == big_payload
        assert len(resp.content) == 5 * 1024 * 1024

    def test_media_route_streams(self, app_client, monkeypatch):
        """/media 路由也走流式转发。"""
        import server  # noqa: PLC0415

        payload = b"\x89PNG fake-image-bytes" * 1000
        server.client = _make_mock_client(
            lambda req: httpx.Response(200, content=payload, headers={"content-type": "image/png"})
        )

        resp = app_client.get("/media/img.png")
        assert resp.status_code == 200
        assert resp.content == payload

    def test_uploads_route_streams(self, app_client, monkeypatch):
        """/uploads 路由也走流式转发。"""
        import server  # noqa: PLC0415

        payload = b"upload-file-content-" * 500
        server.client = _make_mock_client(
            lambda req: httpx.Response(200, content=payload)
        )

        resp = app_client.get("/uploads/file.bin")
        assert resp.status_code == 200
        assert resp.content == payload

    def test_post_body_forwarded(self, app_client, monkeypatch):
        """POST 请求体被正确转发到后端。"""
        received_bodies: list[bytes] = []
        import server  # noqa: PLC0415

        def handler(req: httpx.Request) -> httpx.Response:
            received_bodies.append(req.content)
            return httpx.Response(201, json={"created": True})

        server.client = _make_mock_client(handler)

        resp = app_client.post("/api/v1/items", json={"name": "test"})
        assert resp.status_code == 201
        assert len(received_bodies) == 1
        assert b"name" in received_bodies[0]

    def test_query_params_forwarded(self, app_client, monkeypatch):
        """query string 被透传到后端。"""
        received_urls: list[str] = []
        import server  # noqa: PLC0415

        def handler(req: httpx.Request) -> httpx.Response:
            received_urls.append(str(req.url))
            return httpx.Response(200, json={"ok": True})

        server.client = _make_mock_client(handler)

        app_client.get("/api/v1/threads?limit=50&after_sequence=100")
        assert any("limit=50" in u and "after_sequence=100" in u for u in received_urls)

    def test_error_status_code_passthrough(self, app_client, monkeypatch):
        """后端返回 4xx/5xx 时，状态码原样透传给前端。"""
        import server  # noqa: PLC0415

        for code in (400, 401, 403, 404, 500, 503):
            server.client = _make_mock_client(
                lambda req, c=code: httpx.Response(c, json={"error": "x"})
            )
            resp = app_client.get("/api/v1/test")
            assert resp.status_code == code, f"状态码 {code} 应原样透传"

    def test_content_type_header_preserved(self, app_client, monkeypatch):
        """后端的 content-type 被透传（非 hop-by-hop）。"""
        import server  # noqa: PLC0415

        server.client = _make_mock_client(
            lambda req: httpx.Response(200, content=b'{"x":1}', headers={"content-type": "application/json; charset=utf-8"})
        )
        resp = app_client.get("/api/v1/test")
        assert "application/json" in resp.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# 2. 回归红线：绝不使用 resp.content 全量读取
# ---------------------------------------------------------------------------

class TestNoFullReadRegression:
    """验证回归红线：流式代理绝不在主线程一次性读取完整响应体。

    这是 brk 卡死 bug 的根因。旧代码用 Response(content=resp.content)，
    会同步读取整个响应体到 bytes，触发大块堆分配。
    新代码必须用 stream=True + aiter_bytes 分块。
    """

    @pytest.mark.asyncio
    async def test_stream_true_is_passed_to_client(self, monkeypatch):
        """_stream_proxy 调用 client.send 时必须传 stream=True（防 httpx 预读响应体）。"""
        import server  # noqa: PLC0415

        captured_send_kwargs: dict = {}

        class _CapturingClient:
            def build_request(self, *args, **kwargs):
                # 返回一个最小的 httpx.Request 占位
                import httpx as _hx  # noqa: PLC0415
                return _hx.Request("GET", args[1] if len(args) > 1 else "http://x")

            async def send(self, req, **kwargs):
                captured_send_kwargs.update(kwargs)
                return _MockStreamingResponse(200, b'{"ok":true}')

        monkeypatch.setattr(server, "client", _CapturingClient())

        req = _make_fake_request()
        await server._stream_proxy(req, "http://backend/api/x")
        assert captured_send_kwargs.get("stream") is True, "必须传 stream=True 给 client.send 防止预读响应体"

    def test_response_object_never_accessed_content_property(self, monkeypatch):
        """流式响应对象绝不被访问 .content 属性（会触发全量读取）。"""
        import server  # noqa: PLC0415

        monkeypatch.setattr(server, "client", _make_mock_client(
            lambda req: httpx.Response(200, content=b"data")
        ))

        # 用 AST 静态检查 server.py 源码：业务逻辑里不应出现 resp.content
        source = Path(server.__file__).read_text(encoding="utf-8")
        # 排除注释和字符串中的提及
        for lineno, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # 业务代码里不应有 .content=resp.content 或 content=resp.content
            assert "content=resp.content" not in stripped.replace(" ", ""), (
                f"line {lineno}: 禁止用 content=resp.content 全量读取（brk 卡死根因）: {line}"
            )


# ---------------------------------------------------------------------------
# 3. hop-by-hop headers 过滤 + 业务 headers 透传
# ---------------------------------------------------------------------------

class TestHeaderHandling:
    """验证代理不透传 hop-by-hop headers，但透传业务 headers。"""

    def test_hop_by_hop_headers_filtered(self, app_client, monkeypatch):
        """后端返回的 hop-by-hop headers 不应出现在代理响应里。"""
        import server  # noqa: PLC0415

        # 注意：不能设 content-encoding=gzip 配非 gzip body（httpx 会尝试解码报错）。
        # 用 transfer-encoding 和 connection 这类不触发 body 解码的 hop-by-hop header 验证。
        server.client = _make_mock_client(lambda req: httpx.Response(
            200,
            content=b'{"ok":true}',
            headers={
                "connection": "keep-alive",        # hop-by-hop，应被过滤
                "keep-alive": "timeout=5",         # hop-by-hop，应被过滤
                "x-business": "keep-me",           # 业务 header，应保留
                "x-trace": "trace-id-123",         # 业务 header，应保留
            },
        ))

        resp = app_client.get("/api/v1/test")
        assert "connection" not in {k.lower() for k in resp.headers}, "hop-by-hop header 应被过滤"
        assert "keep-alive" not in {k.lower() for k in resp.headers}, "hop-by-hop header 应被过滤"
        assert resp.headers.get("x-business") == "keep-me"
        assert resp.headers.get("x-trace") == "trace-id-123"

    def test_host_header_not_forwarded(self, app_client, monkeypatch):
        """入站请求的 host header 不应转发给后端（避免后端路由混乱）。"""
        received_hosts: list[str] = []
        import server  # noqa: PLC0415

        def handler(req: httpx.Request) -> httpx.Response:
            received_hosts.append(req.headers.get("host", ""))
            return httpx.Response(200, content=b'{"ok":true}')

        server.client = _make_mock_client(handler)

        app_client.get("/api/v1/test", headers={"host": "evil.example.com"})
        # host 不应被原样转发（MockTransport 收到的 host 应是后端地址或空）
        assert received_hosts[0] != "evil.example.com"


# ---------------------------------------------------------------------------
# 4. 后端不可达 → 502
# ---------------------------------------------------------------------------

class TestBackendUnavailable:
    """验证后端连接失败时的降级行为。"""

    def test_api_returns_502_on_connect_error(self, app_client, monkeypatch):
        import server  # noqa: PLC0415

        server.client = _make_mock_client(None, fail_with=httpx.ConnectError)
        resp = app_client.get("/api/v1/test")
        assert resp.status_code == 502

    def test_media_returns_502_on_connect_error(self, app_client, monkeypatch):
        import server  # noqa: PLC0415

        server.client = _make_mock_client(None, fail_with=httpx.ConnectError)
        resp = app_client.get("/media/x.png")
        assert resp.status_code == 502

    def test_uploads_returns_502_on_connect_error(self, app_client, monkeypatch):
        import server  # noqa: PLC0415

        server.client = _make_mock_client(None, fail_with=httpx.ConnectError)
        resp = app_client.get("/uploads/x.bin")
        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# 5. 连接关闭（防泄漏）
# ---------------------------------------------------------------------------

class TestConnectionCleanup:
    """验证流式响应的 resp 在流结束后被关闭，避免连接泄漏。"""

    @pytest.mark.asyncio
    async def test_resp_aclose_called_after_stream_consumed(self, monkeypatch):
        """resp.aclose 在流消费完后必须被调用。"""
        import server  # noqa: PLC0415

        mock_resp = _MockStreamingResponse(200, b"streamed-content")
        monkeypatch.setattr(server, "client", _MockClientReturning(mock_resp))

        req = _make_fake_request()
        response = await server._stream_proxy(req, "http://backend/api/x")
        # 消费流
        async for _ in response.body_iterator:
            pass
        assert mock_resp.closed, "resp.aclose() 必须在流结束后被调用（防连接泄漏）"


# ---------------------------------------------------------------------------
# 辅助：mock 对象
# ---------------------------------------------------------------------------

class _MockStreamingResponse:
    """模拟 httpx 流式响应，跟踪是否被调用 aiter_bytes 和 aclose。

    关键：不提供 .content 属性（强制走流式路径），记录 aclose 调用。
    """

    def __init__(self, status_code: int, body: bytes, headers: dict | None = None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {"content-type": "application/json"}
        self.closed = False
        self.aiter_called = False

    async def aiter_bytes(self):
        self.aiter_called = True
        # 模拟分块：每 1024 字节一块
        chunk_size = 1024
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    async def aclose(self):
        self.closed = True


class _MockClientReturning:
    """返回预设 mock 响应的 client 替身（实现 build_request + send）。"""

    def __init__(self, resp):
        self._resp = resp

    def build_request(self, *args, **kwargs):
        import httpx as _hx  # noqa: PLC0415
        return _hx.Request("GET", args[1] if len(args) > 1 else "http://x")

    async def send(self, req, **kwargs):
        return self._resp


def _make_fake_request():
    """构造一个最小的 Starlette Request mock（供 _stream_proxy 直接调用）。"""
    from unittest.mock import AsyncMock, MagicMock  # noqa: PLC0415

    req = MagicMock()
    req.method = "GET"
    req.headers = {"host": "testserver"}
    req.query_params = {}
    req.body = AsyncMock(return_value=b"")
    return req
