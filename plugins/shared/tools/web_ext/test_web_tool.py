# @feature: FP-0.2.二 内部模块 manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""web_ext WebTool 单元测试（行覆盖 ≥90% 目标）。

覆盖 plugins/shared/tools/web_ext/tool.py：
1. 构造：默认参数 / 域名集合 / 请求头合并 / 代理环境变量
2. get_tool_definition 工具契约
3. execute 分发：缺 url / 安全检查失败 / 非法 action / get / post / fetch
4. _check_url_security 全分支（协议/黑白名单/DNS 失败/内网 IP/解析异常）
5. 响应处理：_response_too_large_msg / _extract_data（JSON/HTML/纯文本）/
   _http_error_result / _request_failure（超时/HTTP 错误/其他）
6. _http_get / _http_post / _fetch_page 成功与失败路径

外部依赖打桩：httpx.AsyncClient 用伪客户端替换（网络）；DNS 解析用模块级
resolve_hostname_ips / is_private_ip 打桩（真实 DNS 属外部依赖）；trafilatura
未安装于根 venv，以 sys.modules 注入伪模块（懒导入点）。

[来源: 车道实测 web_ext 54.8% → 补测]
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_tool() -> Any:
    """动态加载 tool.py（唯一模块名，避免与裸名 tool 冲突）。"""
    mod_name = "web_ext_tool_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "tool.py")
    assert spec is not None and spec.loader is not None, "Cannot load tool.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_tool()
WebTool = _MOD.WebTool


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── 伪外部依赖 ─────────────────────────────────────────────


class _FakeResponse:
    """伪 httpx.Response：headers/content/status_code/json。"""

    def __init__(
        self,
        content: bytes = b"",
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.content = content
        self.status_code = status_code
        self._headers = headers or {}

    @property
    def headers(self) -> httpx.Headers:
        return httpx.Headers(self._headers)

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))


class _FakeAsyncClient:
    """伪 httpx.AsyncClient：记录调用，按脚本返回响应或抛异常。"""

    def __init__(
        self,
        get_resp: _FakeResponse | Exception | None = None,
        post_resp: _FakeResponse | Exception | None = None,
    ) -> None:
        self._get_resp = get_resp
        self._post_resp = post_resp
        self.get_calls: list[dict[str, Any]] = []
        self.post_calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        pass

    async def get(self, url: str, headers: dict[str, str] | None = None, params: Any = None, timeout: Any = None) -> _FakeResponse:
        self.get_calls.append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        if isinstance(self._get_resp, Exception):
            raise self._get_resp
        assert self._get_resp is not None
        return self._get_resp

    async def post(self, url: str, headers: dict[str, str] | None = None, json: Any = None, params: Any = None, timeout: Any = None) -> _FakeResponse:
        self.post_calls.append({"url": url, "headers": headers, "json": json, "params": params, "timeout": timeout})
        if isinstance(self._post_resp, Exception):
            raise self._post_resp
        assert self._post_resp is not None
        return self._post_resp


class _FakeTrafilatura:
    """伪 trafilatura 模块：extract 按脚本返回或抛异常。"""

    def __init__(self, extracted: str | None = None, exc: Exception | None = None) -> None:
        self._extracted = extracted
        self._exc = exc
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def extract(self, text: str, **kwargs: Any) -> str | None:
        self.calls.append((text, kwargs))
        if self._exc is not None:
            raise self._exc
        return self._extracted


@pytest.fixture
def fake_trafilatura(monkeypatch) -> _FakeTrafilatura:
    fake = _FakeTrafilatura()
    monkeypatch.setitem(sys.modules, "trafilatura", fake)
    return fake


@pytest.fixture
def dns_ok(monkeypatch) -> None:
    """公网 DNS 打桩：解析成功且非内网。"""
    monkeypatch.setattr(_MOD, "resolve_hostname_ips", lambda hostname: (["93.184.216.34"], None))
    monkeypatch.setattr(_MOD, "is_private_ip", lambda ip: False)


def _install_client(monkeypatch, client: _FakeAsyncClient) -> None:
    monkeypatch.setattr(_MOD.httpx, "AsyncClient", lambda **kw: client)


# ═══════════════════════════════════════════════════════════
# 构造
# ═══════════════════════════════════════════════════════════


class TestConstructor:
    def test_defaults(self, monkeypatch) -> None:
        for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            monkeypatch.delenv(var, raising=False)
        tool = WebTool()
        assert tool.timeout == 30
        assert tool.max_response_size == 10 * 1024 * 1024
        assert tool.allowed_domains is None
        assert tool.blocked_domains == set()
        assert tool.verify_ssl is True
        assert tool._proxy_url is None
        # 默认头含浏览器 UA
        assert "User-Agent" in tool._default_headers
        assert "Chrome" in tool._default_headers["User-Agent"]

    def test_custom_args(self) -> None:
        tool = WebTool(
            timeout=5,
            max_response_size=100,
            allowed_domains=["example.com", "github.com"],
            blocked_domains=["ads.com"],
            verify_ssl=False,
            default_headers={"X-Custom": "v1", "User-Agent": "custom-ua"},
        )
        assert tool.timeout == 5
        assert tool.max_response_size == 100
        assert tool.allowed_domains == {"example.com", "github.com"}
        assert tool.blocked_domains == {"ads.com"}
        assert tool.verify_ssl is False
        # 构造期头覆盖模块默认头
        assert tool._default_headers["User-Agent"] == "custom-ua"
        assert tool._default_headers["X-Custom"] == "v1"

    def test_proxy_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
        tool = WebTool()
        assert tool._proxy_url == "http://proxy.local:3128"

    def test_proxy_fallback_to_http_proxy(self, monkeypatch) -> None:
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        monkeypatch.setenv("HTTP_PROXY", "http://proxy2.local:3128")
        tool = WebTool()
        assert tool._proxy_url == "http://proxy2.local:3128"


# ═══════════════════════════════════════════════════════════
# 工具契约
# ═══════════════════════════════════════════════════════════


class TestToolDefinition:
    def test_get_tool_definition(self) -> None:
        definition = WebTool.get_tool_definition()
        assert definition.name == "web_operate"
        assert definition.category.value == "web"
        assert definition.source.value == "code"
        assert "web" in definition.tags
        assert definition.input_schema["required"] == ["action", "url"]
        assert definition.input_schema["properties"]["action"]["enum"] == ["get", "post", "fetch"]
        assert definition.output_schema["required"] == ["status"]


# ═══════════════════════════════════════════════════════════
# execute 分发
# ═══════════════════════════════════════════════════════════


class TestExecuteDispatch:
    def test_missing_url(self) -> None:
        tool = WebTool()
        result = _run(tool.execute({"action": "get"}))
        assert result.success is False
        assert result.error_code == "MISSING_URL"
        assert "URL 不能为空" in result.error

    def test_security_failure(self) -> None:
        tool = WebTool()
        result = _run(tool.execute({"action": "get", "url": "http://127.0.0.1:8080/admin"}))
        assert result.success is False
        assert result.error_code == "SECURITY_CHECK_FAILED"
        assert "URL 安全检查失败" in result.error

    def test_invalid_action(self) -> None:
        tool = WebTool()
        result = _run(tool.execute({"action": "delete", "url": "http://example.com/"}))
        assert result.success is False
        assert result.error_code == "INVALID_ACTION"
        assert "不支持的操作: delete" in result.error

    def test_dispatch_get(self, monkeypatch, dns_ok) -> None:
        client = _FakeAsyncClient(get_resp=_FakeResponse(b'{"ok": true}'))
        _install_client(monkeypatch, client)
        tool = WebTool()
        result = _run(tool.execute({"action": "get", "url": "http://example.com/api"}))
        assert result.success is True
        assert result.output["status"] == 200
        assert result.output["data"] == {"ok": True}
        assert client.get_calls[0]["url"] == "http://example.com/api"

    def test_dispatch_post(self, monkeypatch, dns_ok) -> None:
        client = _FakeAsyncClient(post_resp=_FakeResponse(b'{"id": 1}'))
        _install_client(monkeypatch, client)
        tool = WebTool()
        result = _run(tool.execute({"action": "post", "url": "http://example.com/api", "data": {"a": 1}}))
        assert result.success is True
        assert result.output["data"] == {"id": 1}
        assert client.post_calls[0]["json"] == {"a": 1}

    def test_dispatch_fetch(self, monkeypatch, dns_ok, fake_trafilatura) -> None:
        fake_trafilatura._extracted = "正文内容"
        client = _FakeAsyncClient(get_resp=_FakeResponse("<html><body>正文内容</body></html>".encode("utf-8")))
        _install_client(monkeypatch, client)
        tool = WebTool()
        result = _run(tool.execute({"action": "fetch", "url": "http://example.com/page"}))
        assert result.success is True
        assert result.output["text"] == "正文内容"


# ═══════════════════════════════════════════════════════════
# _check_url_security 全分支
# ═══════════════════════════════════════════════════════════


class TestCheckUrlSecurity:
    def test_unsupported_scheme(self) -> None:
        tool = WebTool()
        ok, msg = tool._check_url_security("ftp://example.com/x")
        assert ok is False and "不支持的协议" in msg

    def test_blocked_domain_exact_and_subdomain(self) -> None:
        tool = WebTool(blocked_domains=["example.com"])
        ok, msg = tool._check_url_security("http://example.com/x")
        assert ok is False and "禁止列表" in msg
        ok2, _ = tool._check_url_security("http://sub.example.com/x")
        assert ok2 is False

    def test_blocked_domain_port_stripped(self) -> None:
        """netloc 带端口 → 域名剥离端口后再比对。"""
        tool = WebTool(blocked_domains=["example.com"])
        ok, _ = tool._check_url_security("http://example.com:8080/x")
        assert ok is False

    def test_allowed_domain_pass_and_fail(self, monkeypatch) -> None:
        monkeypatch.setattr(_MOD, "resolve_hostname_ips", lambda hostname: (["93.184.216.34"], None))
        monkeypatch.setattr(_MOD, "is_private_ip", lambda ip: False)
        tool = WebTool(allowed_domains=["example.com"])
        ok, _ = tool._check_url_security("http://sub.example.com/x")
        assert ok is True
        ok2, msg2 = tool._check_url_security("http://other.com/x")
        assert ok2 is False and "不在允许列表" in msg2

    def test_dns_failure(self, monkeypatch) -> None:
        monkeypatch.setattr(_MOD, "resolve_hostname_ips", lambda hostname: (None, "无法解析域名: nope"))
        tool = WebTool()
        ok, msg = tool._check_url_security("http://nope.invalid/x")
        assert ok is False and "域名解析失败" in msg

    def test_private_ip_rejected(self, monkeypatch) -> None:
        monkeypatch.setattr(_MOD, "resolve_hostname_ips", lambda hostname: (["10.0.0.5"], None))
        monkeypatch.setattr(_MOD, "is_private_ip", lambda ip: True)
        tool = WebTool()
        ok, msg = tool._check_url_security("http://internal.example/x")
        assert ok is False and "内网/回环" in msg

    def test_ok(self, monkeypatch) -> None:
        monkeypatch.setattr(_MOD, "resolve_hostname_ips", lambda hostname: (["93.184.216.34"], None))
        monkeypatch.setattr(_MOD, "is_private_ip", lambda ip: False)
        tool = WebTool()
        ok, msg = tool._check_url_security("http://example.com/x")
        assert ok is True and msg is None

    def test_parse_exception(self) -> None:
        """urlparse 抛异常（畸形 IPv6 字面量）→ 解析失败。"""
        tool = WebTool()
        ok, msg = tool._check_url_security("http://[::1")
        assert ok is False and "URL 解析失败" in msg


# ═══════════════════════════════════════════════════════════
# 纯函数：恢复提示 / 头合并 / 响应大小 / 错误结果
# ═══════════════════════════════════════════════════════════


class TestPureHelpers:
    def test_http_recovery_hint(self) -> None:
        tool = WebTool()
        assert "web_search" in tool._http_recovery_hint(403)
        assert "web_search" in tool._http_recovery_hint(404)
        assert tool._http_recovery_hint(500) == ""

    def test_merge_headers(self) -> None:
        tool = WebTool(default_headers={"X-A": "1"})
        merged = tool._merge_headers({"X-B": "2", "X-A": "override"})
        assert merged["X-A"] == "override"
        assert merged["X-B"] == "2"
        assert "User-Agent" in merged
        # None 调用方头 → 仅默认头
        assert tool._merge_headers(None)["X-A"] == "1"

    def test_response_too_large_by_header(self) -> None:
        tool = WebTool(max_response_size=100)
        resp = _FakeResponse(b"x", headers={"Content-Length": "500"})
        msg = tool._response_too_large_msg(resp)
        assert msg is not None and "响应过大" in msg and "500" in msg

    def test_response_too_large_by_content(self) -> None:
        tool = WebTool(max_response_size=100)
        resp = _FakeResponse(b"x" * 200)  # 无 Content-Length 头
        msg = tool._response_too_large_msg(resp)
        assert msg is not None and "200" in msg

    def test_response_size_ok(self) -> None:
        tool = WebTool(max_response_size=100)
        resp = _FakeResponse(b"x" * 50, headers={"Content-Length": "50"})
        assert tool._response_too_large_msg(resp) is None

    def test_http_error_result_with_payload_and_hint(self) -> None:
        tool = WebTool()
        resp = _FakeResponse(b"", status_code=403)
        result = tool._http_error_result(resp, "forbidden")
        assert result.success is False
        assert result.error_code == "HTTP_403"
        assert "HTTP 403: forbidden" in result.error
        assert "该网站拒绝访问" in result.error

    def test_http_error_result_without_payload(self) -> None:
        tool = WebTool()
        resp = _FakeResponse(b"", status_code=500)
        result = tool._http_error_result(resp)
        assert result.error == "HTTP 500"
        assert result.error_code == "HTTP_500"

    def test_request_failure_timeout(self) -> None:
        tool = WebTool()
        result = tool._request_failure(httpx.TimeoutException("slow"), "GET 请求失败", "GET_FAILED")
        assert result.error == "请求超时"
        assert result.error_code == "TIMEOUT"

    def test_request_failure_http_error(self) -> None:
        tool = WebTool()
        result = tool._request_failure(httpx.ConnectError("refused"), "GET 请求失败", "GET_FAILED")
        assert result.error_code == "HTTP_ERROR"
        assert "refused" in result.error

    def test_request_failure_generic(self) -> None:
        tool = WebTool()
        result = tool._request_failure(RuntimeError("boom"), "POST 请求失败", "POST_FAILED")
        assert result.error_code == "POST_FAILED"
        assert "POST 请求失败: boom" in result.error


# ═══════════════════════════════════════════════════════════
# _extract_data：JSON / HTML / 纯文本
# ═══════════════════════════════════════════════════════════


class TestExtractData:
    def test_json_parsed(self) -> None:
        tool = WebTool()
        resp = _FakeResponse(b'{"k": [1, 2]}')
        assert tool._extract_data(resp) == {"k": [1, 2]}

    def test_html_extracted(self, fake_trafilatura) -> None:
        fake_trafilatura._extracted = "抽取正文"
        tool = WebTool()
        resp = _FakeResponse("<html><body><p>抽取正文</p></body></html>".encode("utf-8"))
        assert tool._extract_data(resp) == "抽取正文"
        assert fake_trafilatura.calls[0][1]["include_tables"] is True

    def test_html_extract_none_falls_back_to_snippet(self, fake_trafilatura) -> None:
        fake_trafilatura._extracted = None
        tool = WebTool()
        html = "<html><body>长正文</body></html>"
        resp = _FakeResponse(html.encode("utf-8"))
        assert tool._extract_data(resp) == html[:2000]

    def test_html_extract_raises_falls_back(self, fake_trafilatura) -> None:
        fake_trafilatura._exc = RuntimeError("extract failed")
        tool = WebTool()
        html = "<html><body>长正文</body></html>"
        resp = _FakeResponse(html.encode("utf-8"))
        assert tool._extract_data(resp) == html[:2000]

    def test_plain_text_returned(self) -> None:
        tool = WebTool()
        resp = _FakeResponse("纯文本内容".encode("utf-8"))
        assert tool._extract_data(resp) == "纯文本内容"

    def test_doctype_html_detected(self, fake_trafilatura) -> None:
        fake_trafilatura._extracted = "x"
        tool = WebTool()
        resp = _FakeResponse(b"<!DOCTYPE html><html></html>")
        assert tool._extract_data(resp) == "x"


# ═══════════════════════════════════════════════════════════
# _http_get
# ═══════════════════════════════════════════════════════════


class TestHttpGet:
    def test_success_json(self, monkeypatch, dns_ok) -> None:
        client = _FakeAsyncClient(get_resp=_FakeResponse(b'{"ok": true}'))
        _install_client(monkeypatch, client)
        tool = WebTool()
        result = _run(tool._http_get({"url": "http://example.com/api", "params": {"q": "1"}, "timeout": 5}))
        assert result.success is True
        assert result.output == {"status": 200, "data": {"ok": True}}
        assert result.metadata["action"] == "http_get"
        call = client.get_calls[0]
        assert call["params"] == {"q": "1"}
        assert isinstance(call["timeout"], httpx.Timeout) and call["timeout"].read == 5
        # 默认头已合并进请求
        assert "User-Agent" in call["headers"]

    def test_success_html_extracted(self, monkeypatch, dns_ok, fake_trafilatura) -> None:
        fake_trafilatura._extracted = "网页正文"
        client = _FakeAsyncClient(get_resp=_FakeResponse("<html><body>网页正文</body></html>".encode("utf-8")))
        _install_client(monkeypatch, client)
        tool = WebTool()
        result = _run(tool._http_get({"url": "http://example.com/page"}))
        assert result.success is True
        assert result.output["data"] == "网页正文"

    def test_http_400_with_json_payload(self, monkeypatch, dns_ok) -> None:
        client = _FakeAsyncClient(get_resp=_FakeResponse(b'{"error": "bad"}', status_code=400))
        _install_client(monkeypatch, client)
        tool = WebTool()
        result = _run(tool._http_get({"url": "http://example.com/api"}))
        assert result.success is False
        assert result.error_code == "HTTP_400"
        assert "{'error': 'bad'}" in result.error

    def test_too_large_by_header(self, monkeypatch, dns_ok) -> None:
        client = _FakeAsyncClient(get_resp=_FakeResponse(b"x", headers={"Content-Length": "99999999"}))
        _install_client(monkeypatch, client)
        tool = WebTool()
        result = _run(tool._http_get({"url": "http://example.com/big"}))
        assert result.success is False
        assert result.error_code == "RESPONSE_TOO_LARGE"

    def test_too_large_by_content(self, monkeypatch, dns_ok) -> None:
        client = _FakeAsyncClient(get_resp=_FakeResponse(b"x" * 200))
        _install_client(monkeypatch, client)
        tool = WebTool(max_response_size=100)
        result = _run(tool._http_get({"url": "http://example.com/big"}))
        assert result.success is False
        assert result.error_code == "RESPONSE_TOO_LARGE"

    def test_timeout(self, monkeypatch, dns_ok) -> None:
        client = _FakeAsyncClient(get_resp=httpx.TimeoutException("slow"))
        _install_client(monkeypatch, client)
        tool = WebTool()
        result = _run(tool._http_get({"url": "http://example.com/"}))
        assert result.success is False
        assert result.error_code == "TIMEOUT"

    def test_connect_error(self, monkeypatch, dns_ok) -> None:
        client = _FakeAsyncClient(get_resp=httpx.ConnectError("refused"))
        _install_client(monkeypatch, client)
        tool = WebTool()
        result = _run(tool._http_get({"url": "http://example.com/"}))
        assert result.success is False
        assert result.error_code == "HTTP_ERROR"

    def test_generic_error(self, monkeypatch, dns_ok) -> None:
        client = _FakeAsyncClient(get_resp=RuntimeError("boom"))
        _install_client(monkeypatch, client)
        tool = WebTool()
        result = _run(tool._http_get({"url": "http://example.com/"}))
        assert result.success is False
        assert result.error_code == "GET_FAILED"
        assert "GET 请求失败: boom" in result.error


# ═══════════════════════════════════════════════════════════
# _http_post
# ═══════════════════════════════════════════════════════════


class TestHttpPost:
    def test_success(self, monkeypatch, dns_ok) -> None:
        client = _FakeAsyncClient(post_resp=_FakeResponse(b'{"id": 7}'))
        _install_client(monkeypatch, client)
        tool = WebTool()
        result = _run(tool._http_post({"url": "http://example.com/api", "data": {"a": 1}, "params": {"v": "2"}}))
        assert result.success is True
        assert result.output == {"status": 200, "data": {"id": 7}}
        assert result.metadata["action"] == "http_post"
        call = client.post_calls[0]
        assert call["json"] == {"a": 1}
        assert call["params"] == {"v": "2"}

    def test_http_400(self, monkeypatch, dns_ok) -> None:
        client = _FakeAsyncClient(post_resp=_FakeResponse(b"invalid", status_code=422))
        _install_client(monkeypatch, client)
        tool = WebTool()
        result = _run(tool._http_post({"url": "http://example.com/api", "data": {}}))
        assert result.success is False
        assert result.error_code == "HTTP_422"
        assert "invalid" in result.error

    def test_too_large(self, monkeypatch, dns_ok) -> None:
        client = _FakeAsyncClient(post_resp=_FakeResponse(b"x" * 200))
        _install_client(monkeypatch, client)
        tool = WebTool(max_response_size=100)
        result = _run(tool._http_post({"url": "http://example.com/api", "data": {}}))
        assert result.success is False
        assert result.error_code == "RESPONSE_TOO_LARGE"

    def test_generic_error(self, monkeypatch, dns_ok) -> None:
        client = _FakeAsyncClient(post_resp=RuntimeError("boom"))
        _install_client(monkeypatch, client)
        tool = WebTool()
        result = _run(tool._http_post({"url": "http://example.com/api", "data": {}}))
        assert result.success is False
        assert result.error_code == "POST_FAILED"


# ═══════════════════════════════════════════════════════════
# _fetch_page
# ═══════════════════════════════════════════════════════════


class TestFetchPage:
    def test_extract_text_success(self, monkeypatch, dns_ok, fake_trafilatura) -> None:
        fake_trafilatura._extracted = "正文"
        client = _FakeAsyncClient(get_resp=_FakeResponse("<html><body>正文</body></html>".encode("utf-8")))
        _install_client(monkeypatch, client)
        tool = WebTool()
        result = _run(tool._fetch_page({"url": "http://example.com/page"}))
        assert result.success is True
        assert result.output == {"status": 200, "text": "正文"}
        assert result.metadata["action"] == "fetch_page"

    def test_extract_none_warns(self, monkeypatch, dns_ok, fake_trafilatura, caplog) -> None:
        fake_trafilatura._extracted = None
        client = _FakeAsyncClient(get_resp=_FakeResponse(b"<html></html>"))
        _install_client(monkeypatch, client)
        tool = WebTool()
        with caplog.at_level(logging.WARNING):
            result = _run(tool._fetch_page({"url": "http://example.com/page"}))
        assert result.success is True
        assert result.output["text"] == ""
        assert any("未能提取正文" in r.getMessage() for r in caplog.records)

    def test_extract_text_false_returns_html(self, monkeypatch, dns_ok) -> None:
        html = "<html><body>原始 HTML</body></html>"
        client = _FakeAsyncClient(get_resp=_FakeResponse(html.encode("utf-8")))
        _install_client(monkeypatch, client)
        tool = WebTool()
        result = _run(tool._fetch_page({"url": "http://example.com/page", "extract_text": False}))
        assert result.success is True
        assert result.output["html"] == html
        assert "text" not in result.output

    def test_http_404(self, monkeypatch, dns_ok, fake_trafilatura) -> None:
        fake_trafilatura._extracted = None
        client = _FakeAsyncClient(get_resp=_FakeResponse(b"<html>404</html>", status_code=404))
        _install_client(monkeypatch, client)
        tool = WebTool()
        result = _run(tool._fetch_page({"url": "http://example.com/missing"}))
        assert result.success is False
        assert result.error_code == "HTTP_404"
        assert "页面不存在" in result.error

    def test_too_large(self, monkeypatch, dns_ok) -> None:
        client = _FakeAsyncClient(get_resp=_FakeResponse(b"x" * 200))
        _install_client(monkeypatch, client)
        tool = WebTool(max_response_size=100)
        result = _run(tool._fetch_page({"url": "http://example.com/page"}))
        assert result.success is False
        assert result.error_code == "RESPONSE_TOO_LARGE"

    def test_generic_error(self, monkeypatch, dns_ok) -> None:
        client = _FakeAsyncClient(get_resp=RuntimeError("boom"))
        _install_client(monkeypatch, client)
        tool = WebTool()
        result = _run(tool._fetch_page({"url": "http://example.com/page"}))
        assert result.success is False
        assert result.error_code == "FETCH_FAILED"
        assert "抓取网页失败: boom" in result.error
