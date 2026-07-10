"""WebTool 默认请求头注入测试。

验证：
1. 调用方未传 headers 时，注入浏览器默认请求头（含 User-Agent）。
2. 调用方传入的 headers 优先覆盖默认头。
3. fetch.yaml 配置的 default_headers 能合并/覆盖模块默认头。
4. get/post/fetch 三个动作均生效。
"""
from __future__ import annotations

import pytest

from tools.builtin.web.tool import WebTool, _DEFAULT_HEADERS


class _MockResponse:
    """模拟 httpx 响应，返回一段 HTML。"""

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self.content = b"<html><head><title>x</title></head><body><p>hello</p></body></html>"

    def json(self):  # noqa: ANN001, ANN201
        raise ValueError("not json")


class _MockClient:
    """模拟 httpx.AsyncClient 上下文管理器，记录实际请求头与请求体。"""

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN001, ANN002, ANN003
        self._response = _MockResponse()
        self.captured: dict = {}

    async def __aenter__(self) -> "_MockClient":
        return self

    async def __aexit__(self, *args) -> bool:  # noqa: ANN001, ANN002
        return False

    async def get(self, url, headers=None, params=None, timeout=None):  # noqa: ANN001, ANN201, ARG002
        self.captured = {"url": str(url), "headers": dict(headers or {}), "json_body": None}
        return self._response

    async def post(self, url, headers=None, json=None, params=None, timeout=None):  # noqa: ANN001, ANN201, ARG002
        self.captured = {"url": str(url), "headers": dict(headers or {}), "json_body": json}
        return self._response


@pytest.fixture
def mock_clients():
    """捕获所有 httpx.AsyncClient 实例，供断言实际请求头。

    patch httpx.AsyncClient 后，每次 ``httpx.AsyncClient(...)`` 都会创建一个
    _MockClient 并记录到 instances，测试通过 instances[-1].captured 断言。
    """
    instances: list[_MockClient] = []

    class _ProxyClient(_MockClient):
        def __init__(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            super().__init__(*args, **kwargs)
            instances.append(self)

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr("tools.builtin.web.tool.httpx.AsyncClient", _ProxyClient)
        yield instances


async def test_get_injects_default_headers_when_caller_omits_them(mock_clients):
    """get 动作：未传 headers 时注入默认浏览器头。"""
    tool = WebTool()
    await tool.execute({"action": "get", "url": "https://example.com"})

    assert mock_clients, "应创建 httpx client"
    sent = mock_clients[-1].captured["headers"]
    assert "Mozilla" in sent["User-Agent"]
    assert "python-httpx" not in sent["User-Agent"]
    assert "Accept" in sent
    assert "Accept-Language" in sent


async def test_post_injects_default_headers(mock_clients):
    """post 动作：未传 headers 时注入默认头，且请求体被发送。"""
    tool = WebTool()
    await tool.execute(
        {"action": "post", "url": "https://example.com", "data": {"k": "v"}}
    )

    sent = mock_clients[-1].captured
    assert "Mozilla" in sent["headers"]["User-Agent"]
    assert sent["json_body"] == {"k": "v"}


async def test_fetch_injects_default_headers(mock_clients):
    """fetch 动作：未传 headers 时注入默认头。

    用 extract_text=False 跳过 trafilatura，使测试仅聚焦请求头断言、不依赖第三方库。
    """
    tool = WebTool()
    await tool.execute({"action": "fetch", "url": "https://example.com", "extract_text": False})

    sent = mock_clients[-1].captured["headers"]
    assert "Mozilla" in sent["User-Agent"]
    assert "Accept-Language" in sent


async def test_caller_headers_override_defaults(mock_clients):
    """调用方传入的 headers 优先覆盖默认头。"""
    tool = WebTool()
    custom_ua = "MyBot/1.0"
    await tool.execute(
        {"action": "get", "url": "https://example.com", "headers": {"User-Agent": custom_ua, "X-Custom": "yes"}}
    )

    sent = mock_clients[-1].captured["headers"]
    assert sent["User-Agent"] == custom_ua  # 覆盖默认
    assert sent["X-Custom"] == "yes"  # 新增头保留
    assert "Accept" in sent  # 默认头仍存在


async def test_caller_partial_headers_keep_other_defaults(mock_clients):
    """调用方只覆盖部分头，其余默认头保留。"""
    tool = WebTool()
    await tool.execute(
        {"action": "get", "url": "https://example.com", "headers": {"X-Only": "1"}}
    )

    sent = mock_clients[-1].captured["headers"]
    assert sent["X-Only"] == "1"
    assert "Mozilla" in sent["User-Agent"]


def test_default_headers_constant_has_browser_ua():
    """模块默认头常量本身含浏览器 UA，作为无配置时的兜底。"""
    assert "Mozilla" in _DEFAULT_HEADERS["User-Agent"]
    assert "Accept-Language" in _DEFAULT_HEADERS


def test_constructor_default_headers_merge_over_constant():
    """构造期传入的 default_headers 合并到模块默认头之上。"""
    tool = WebTool(default_headers={"X-From-Config": "cfg", "Accept-Language": "en-US"})
    # 模块默认头仍在
    assert "Mozilla" in tool._default_headers["User-Agent"]
    # 配置新增头生效
    assert tool._default_headers["X-From-Config"] == "cfg"
    # 配置覆盖模块默认头
    assert tool._default_headers["Accept-Language"] == "en-US"


async def test_no_config_still_has_browser_ua(mock_clients):
    """无配置构造（cls()）也带浏览器 UA，覆盖默认实例路径。"""
    tool = WebTool()
    await tool.execute({"action": "get", "url": "https://example.com"})
    sent = mock_clients[-1].captured["headers"]
    assert "Mozilla" in sent["User-Agent"]
