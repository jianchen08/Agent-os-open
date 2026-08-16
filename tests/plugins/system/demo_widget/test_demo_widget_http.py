# @feature: FP-0.2.三 宿主接入 | @vision: V3 可嵌入 | @ci: python-coverage
"""demo_widget http.handle 端点单元测试。

http_handle 是 webview widget 的 HTML 资源端点：
返回 ToolExecutionResult 结构 {success, data: {status, headers, body, body_encoding}}。
body 为 base64 编码的 HTML（因内核 http_dispatcher 无条件 base64 decode）。

测试覆盖：返回结构正确、status 200、Content-Type 头、body 可 base64 解码为
含关键标记的 HTML（counter/postMessage/widget.event）、不同入参不影响 HTML 模板。
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

pytestmark = pytest.mark.unit


# ============================================================
# 辅助
# ============================================================


async def _call(**kwargs: Any) -> dict[str, Any]:
    """调用 server.http_handle 并返回结果。"""
    from server import http_handle

    return await http_handle(**kwargs)


def _decode_body(result: dict[str, Any]) -> str:
    """从 ToolExecutionResult 解码出 HTML 文本。"""
    body_b64 = result["data"]["body"]
    assert result["data"].get("body_encoding") == "base64"
    return base64.b64decode(body_b64).decode("utf-8")


# ============================================================
# 模块结构
# ============================================================


class TestModuleStructure:
    def test_plugin已注册且名称正确(self) -> None:
        import server

        assert server.plugin is not None
        # AgentOSPlugin 的 id 属性
        assert getattr(server.plugin, "id", None) == "demo_widget_plugin" or \
            getattr(server.plugin, "name", None) == "demo_widget_plugin"

    def test_http_handle是可调用对象(self) -> None:
        import server

        assert callable(server.http_handle)

    def test_模块级counter初始为0(self) -> None:
        # 重新导入前 counter 可能已被其他测试改变，仅验证它是 int
        import server

        assert isinstance(server._counter, int)


# ============================================================
# http_handle 返回结构
# ============================================================


class TestHttpResponse:
    @pytest.mark.asyncio
    async def test_返回success为True(self) -> None:
        result = await _call()
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_status_200(self) -> None:
        result = await _call()
        assert result["data"]["status"] == 200

    @pytest.mark.asyncio
    async def test_Content_Type为text_html(self) -> None:
        result = await _call()
        ct = result["data"]["headers"]["Content-Type"]
        assert "text/html" in ct
        assert "charset=utf-8" in ct

    @pytest.mark.asyncio
    async def test_body_encoding字段为base64(self) -> None:
        result = await _call()
        assert result["data"]["body_encoding"] == "base64"

    @pytest.mark.asyncio
    async def test_body是合法base64字符串(self) -> None:
        result = await _call()
        body = result["data"]["body"]
        assert isinstance(body, str)
        # 合法 base64：能解码不抛
        decoded = base64.b64decode(body)
        assert isinstance(decoded, bytes)
        assert len(decoded) > 0


# ============================================================
# HTML 内容正确性
# ============================================================


class TestHtmlContent:
    @pytest.mark.asyncio
    async def test_html含DOCTYPE与基本结构(self) -> None:
        html = _decode_body(await _call())
        assert "<!DOCTYPE html>" in html
        assert "<html>" in html
        assert "</html>" in html

    @pytest.mark.asyncio
    async def test_html含计数器展示元素(self) -> None:
        html = _decode_body(await _call())
        assert 'id="cnt"' in html
        assert "counter" in html  # class 名

    @pytest.mark.asyncio
    async def test_html含postMessage上行调用脚本(self) -> None:
        html = _decode_body(await _call())
        assert "window.agentos" in html
        assert "postMessage" in html
        assert "demo.ping" in html

    @pytest.mark.asyncio
    async def test_html含widget_event下行监听(self) -> None:
        html = _decode_body(await _call())
        assert "widget.event" in html
        assert "addEventListener" in html

    @pytest.mark.asyncio
    async def test_html含标题Demo字样(self) -> None:
        html = _decode_body(await _call())
        assert "Demo" in html


# ============================================================
# 入参无关性
# ============================================================


class TestParameterAgnostic:
    @pytest.mark.asyncio
    async def test_不同path返回相同HTML(self) -> None:
        r1 = _decode_body(await _call(path="/webview"))
        r2 = _decode_body(await _call(path="/other"))
        assert r1 == r2

    @pytest.mark.asyncio
    async def test_POST方法也返回相同HTML(self) -> None:
        r_get = _decode_body(await _call(method="GET"))
        r_post = _decode_body(await _call(method="POST", raw_body="x"))
        assert r_get == r_post

    @pytest.mark.asyncio
    async def test_带headers和query不影响结果(self) -> None:
        r1 = _decode_body(await _call())
        r2 = _decode_body(
            await _call(
                headers={"X-Custom": "v"},
                query={"foo": "bar"},
                plugin_id="demo_widget_plugin",
            )
        )
        assert r1 == r2

    @pytest.mark.asyncio
    async def test_默认参数调用不抛(self) -> None:
        # 不传任何参数（全用默认值）
        result = await _call()
        assert result["success"] is True
