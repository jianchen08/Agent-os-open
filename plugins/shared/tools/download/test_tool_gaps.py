# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""download 工具缺口分支补充测试（coverage 冲刺批九）。

对齐 coverage.xml 2026-09-13 缺行口径，只补既有 test_download.py /
test_workspace_aware.py 未覆盖的分支：
- 纯函数兜底：Content-Disposition 头无 filename 参数（tool.py:99）
- proxy 透传 httpx 客户端构造（tool.py:349）
- 续传状态文件损坏 → 忽略续传全量重下（tool.py:404-406）
- 分片大小不匹配 / 分片重试退避 / 分片重定向安全拒绝不重试
  （tool.py:521, 528-537）
- 流式续传遇 200 全量重写、已完整 .tmp 直接落位、流式超限重试、
  流式重定向安全拒绝、流式重试退避（tool.py:596-597, 614, 625, 643, 646-648）
- 重定向响应缺 Location 头、HEAD 请求退避重试（tool.py:698, 728-730）
- 公开 schema 隐藏 skip_ssrf_check 的安全契约（tool.py:139）

网络边界全部以伪 httpx.AsyncClient 脚本化（无真实网络）；时钟边界
（重试退避 asyncio.sleep）注入记录式 fake sleep，不真实等待。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/tools/download/
_TOOLS_DIR = _PLUGIN_DIR.parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


def _load_tool() -> Any:
    mod_name = "download_tool_gaps_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "tool.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_tool()
DownloadTool = _MOD.DownloadTool
_extract_filename_from_headers = _MOD._extract_filename_from_headers

_URL = "http://example.com/gaps.bin"
_URL_BIG = "http://example.com/big.bin"


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ═══════════════════════════════════════════════════════════
# 伪 httpx 件：脚本化响应客户端 + 可脚本化响应
# ═══════════════════════════════════════════════════════════


class _FakeResp:
    """伪 httpx.Response：内容/头/状态码可脚本化。"""

    def __init__(
        self,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
        status_code: int = 200,
    ) -> None:
        self._content = content
        self._headers = headers or {}
        self.status_code = status_code

    @property
    def headers(self) -> httpx.Headers:
        return httpx.Headers(self._headers)

    def raise_for_status(self) -> None:
        # 测试脚本不用 ≥400 状态触发失败——错误注入一律走脚本化异常
        # （httpx.ConnectError 等），本 fake 只承载 2xx/3xx 响应。
        pass

    async def aiter_bytes(self, chunk_size: int) -> Any:
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i : i + chunk_size]


class _ScriptedClient:
    """按 (METHOD, url) 脚本化响应的伪 httpx.AsyncClient。

    脚本值为 _FakeResp 或异常实例；列表按调用顺序出队消费，仅一项时重复使用。
    """

    def __init__(self, script: dict[tuple[str, str], list[Any]]) -> None:
        self._script = script
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    async def __aenter__(self) -> _ScriptedClient:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def request(self, method: str, url: str, headers: dict | None = None, **kwargs: Any) -> Any:
        key = (method.upper(), url)
        self.requests.append((method.upper(), url, dict(headers or {})))
        outcomes = self._script[key]
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _install_client(monkeypatch, client: _ScriptedClient) -> dict[str, Any]:
    """以伪客户端替换 httpx.AsyncClient，返回捕获到的构造参数。"""
    captured: dict[str, Any] = {}

    def factory(**kwargs: Any) -> _ScriptedClient:
        captured.update(kwargs)
        return client

    monkeypatch.setattr(_MOD.httpx, "AsyncClient", factory)
    return captured


def _patch_validate_reject(monkeypatch, marker: str) -> None:
    """validate_url 替身：URL 含 marker 即拒绝（其余放行），免真实 DNS。"""

    def fake_validate(url: str, allow_domains: list[str] | None = None) -> tuple[bool, str]:
        if marker in url:
            return False, f"域名 {marker} 被测试策略拒绝（SSRF 防护）"
        return True, "OK"

    monkeypatch.setattr(_MOD, "validate_url", fake_validate)


@pytest.fixture
def fake_sleep(monkeypatch):
    """记录式退避时钟：吞掉真实等待，记录每次请求的延迟秒数。"""
    calls: list[float] = []

    async def _sleep(seconds: float, *args: Any, **kwargs: Any) -> None:
        calls.append(seconds)

    monkeypatch.setattr(_MOD.asyncio, "sleep", _sleep)
    return calls


def _head_meta(client: _ScriptedClient, length: int, etag: str = '"e1"') -> None:
    client._script[("HEAD", _URL)] = [
        _FakeResp(b"", {"content-length": str(length), "accept-ranges": "bytes", "etag": etag})
    ]


# ═══════════════════════════════════════════════════════════
# 纯函数兜底：Content-Disposition 无 filename
# ═══════════════════════════════════════════════════════════


class TestHeaderFilenameFallback:
    @pytest.mark.parametrize(
        "content_disposition",
        ["attachment", "inline; name=report.txt", "attachment; size=1024"],
        ids=["bare-attachment", "name-only", "other-param"],
    )
    def test_content_disposition_without_filename_returns_none(self, content_disposition: str) -> None:
        """头存在但不含 filename 参数 → None（交由 URL 提取兜底）。"""
        headers = httpx.Headers({"content-disposition": content_disposition})
        assert _extract_filename_from_headers(headers) is None


# ═══════════════════════════════════════════════════════════
# proxy 透传 httpx 客户端构造
# ═══════════════════════════════════════════════════════════


class TestProxyForwarding:
    @pytest.mark.parametrize(
        ("proxy", "should_forward"),
        [("http://127.0.0.1:7890", True), (None, False)],
        ids=["with-proxy", "without-proxy"],
    )
    def test_proxy_forwarded_to_http_client(
        self, tmp_path: Path, monkeypatch, proxy: str | None, should_forward: bool
    ) -> None:
        content = b"proxy-check"
        client = _ScriptedClient(
            {
                ("HEAD", _URL): [_FakeResp(b"", {"content-length": str(len(content))})],
                ("GET", _URL): [_FakeResp(content)],
            }
        )
        captured = _install_client(monkeypatch, client)
        tool = DownloadTool()
        result = _run(
            tool._download(
                url=_URL,
                save_dir=tmp_path,
                filename="pc.bin",
                max_connections=1,
                max_retries=1,
                timeout=5,
                max_size=0,
                proxy=proxy,
            )
        )
        assert result["size"] == len(content)
        assert ("proxy" in captured) is should_forward
        if should_forward:
            assert captured["proxy"] == proxy
        # 手动逐跳重定向契约：客户端绝不自动跟随（每跳 SSRF 复检的前提）
        assert captured["follow_redirects"] is False


# ═══════════════════════════════════════════════════════════
# 续传状态文件损坏 → 忽略续传全量重下
# ═══════════════════════════════════════════════════════════


class TestCorruptStateFile:
    @pytest.mark.parametrize(
        "corrupt_body",
        ['{"etag": "e1"', "{not-json}", ""],
        ids=["truncated-json", "garbage", "empty"],
    )
    def test_corrupt_state_ignored_full_redownload(
        self, tmp_path: Path, monkeypatch, caplog, corrupt_body: str
    ) -> None:
        payload = bytes(range(16))
        client = _ScriptedClient({("GET", _URL): [_FakeResp(payload)]})
        _head_meta(client, len(payload))
        _install_client(monkeypatch, client)
        (tmp_path / "cs.bin.state.json").write_text(corrupt_body, encoding="utf-8")

        tool = DownloadTool()
        with caplog.at_level(logging.WARNING):
            result = _run(
                tool._download(
                    url=_URL,
                    save_dir=tmp_path,
                    filename="cs.bin",
                    max_connections=2,
                    max_retries=2,
                    timeout=5,
                    max_size=0,
                    proxy=None,
                )
            )

        assert result["size"] == len(payload)
        assert (tmp_path / "cs.bin").read_bytes() == payload
        assert result["resumed"] is False
        # 损坏状态被忽略：分片按完整 Range 重取
        assert any(h.get("Range") == "bytes=0-15" for _, _, h in client.requests)
        # 降级留痕可见，状态文件随成功清理
        assert any("状态文件损坏" in r.getMessage() for r in caplog.records)
        assert not (tmp_path / "cs.bin.state.json").exists()


# ═══════════════════════════════════════════════════════════
# 分片获取失败：大小不匹配 / 退避重试 / 重定向安全拒绝
# ═══════════════════════════════════════════════════════════


class TestSegmentFetchFailures:
    @pytest.mark.parametrize("short_body", [b"", b"abc"], ids=["empty", "short"])
    def test_segment_size_mismatch_raises_without_retry(
        self, tmp_path: Path, monkeypatch, short_body: bytes
    ) -> None:
        client = _ScriptedClient({("GET", _URL): [_FakeResp(short_body)]})
        _head_meta(client, 8)
        _install_client(monkeypatch, client)
        tool = DownloadTool()
        with pytest.raises(RuntimeError, match="大小不匹配"):
            _run(
                tool._download(
                    url=_URL,
                    save_dir=tmp_path,
                    filename="mm.bin",
                    max_connections=1,
                    max_retries=1,
                    timeout=5,
                    max_size=0,
                    proxy=None,
                )
            )
        # max_retries=1：单次失败即终止，不重试
        assert len([r for r in client.requests if r[0] == "GET"]) == 1

    def test_segment_retry_with_backoff_then_success(
        self, tmp_path: Path, monkeypatch, fake_sleep
    ) -> None:
        payload = bytes(range(8))
        client = _ScriptedClient(
            {("GET", _URL): [httpx.ConnectError("refused"), _FakeResp(payload)]}
        )
        _head_meta(client, len(payload))
        _install_client(monkeypatch, client)
        tool = DownloadTool()
        result = _run(
            tool._download(
                url=_URL,
                save_dir=tmp_path,
                filename="rb.bin",
                max_connections=1,
                max_retries=2,
                timeout=5,
                max_size=0,
                proxy=None,
            )
        )
        assert (tmp_path / "rb.bin").read_bytes() == payload
        assert result["size"] == len(payload)
        # 重试请求同一分片的同一 Range
        get_headers = [h for m, _, h in client.requests if m == "GET"]
        assert len(get_headers) == 2
        assert all(h.get("Range") == "bytes=0-7" for h in get_headers)
        # 首次重试退避 2**1 秒，且始终有界（≤30s 上限）
        assert fake_sleep == [2]
        assert all(0 < w <= 30 for w in fake_sleep)

    def test_segment_redirect_security_rejected_no_retry(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _patch_validate_reject(monkeypatch, "blocked.example")
        client = _ScriptedClient(
            {
                ("GET", _URL): [
                    _FakeResp(b"", {"location": "http://blocked.example/evil"}, status_code=302)
                ]
            }
        )
        _head_meta(client, 8)
        _install_client(monkeypatch, client)
        tool = DownloadTool()
        with pytest.raises(_MOD.RedirectSecurityError, match="重定向目标被拒绝"):
            _run(
                tool._download(
                    url=_URL,
                    save_dir=tmp_path,
                    filename="sr.bin",
                    max_connections=1,
                    max_retries=3,
                    timeout=5,
                    max_size=0,
                    proxy=None,
                )
            )
        # 安全拒绝不重试：即便重试额度充足，GET 也只发生一次
        assert len([r for r in client.requests if r[0] == "GET"]) == 1


# ═══════════════════════════════════════════════════════════
# 流式下载分支：200 重写 / .tmp 已完整 / 超限重试 / 安全拒绝
# ═══════════════════════════════════════════════════════════


class TestStreamDownloadBranches:
    @pytest.mark.parametrize(
        ("resp_status", "resp_body", "expect_resumed", "expect_final"),
        [
            (200, b"x" * 100, False, b"x" * 100),
            (206, b"y" * 60, True, b"z" * 40 + b"y" * 60),
        ],
        ids=["server-ignores-range", "server-honors-range"],
    )
    def test_resume_request_200_rewrites_from_scratch(
        self,
        tmp_path: Path,
        monkeypatch,
        resp_status: int,
        resp_body: bytes,
        expect_resumed: bool,
        expect_final: bytes,
    ) -> None:
        """续传请求遇 200 全量响应 → 丢弃已下部分从头写（防内容翻倍）；
        遇 206 → 追加续传。同一初始状态、两种服务器行为，结果都恰好完整。"""
        client = _ScriptedClient(
            {
                ("HEAD", _URL): [_FakeResp(b"", {"content-length": "100"})],
                ("GET", _URL): [_FakeResp(resp_body, status_code=resp_status)],
            }
        )
        _install_client(monkeypatch, client)
        tmp_file = tmp_path / "rw.bin.tmp"
        tmp_file.write_bytes(b"z" * 40)

        tool = DownloadTool()
        result = _run(
            tool._download(
                url=_URL,
                save_dir=tmp_path,
                filename="rw.bin",
                max_connections=1,
                max_retries=2,
                timeout=5,
                max_size=0,
                proxy=None,
            )
        )

        assert (tmp_path / "rw.bin").read_bytes() == expect_final
        assert result["size"] == 100
        assert result["resumed"] is expect_resumed
        # 续传以 Range 请求发起（服务器是否配合由响应状态决定）
        get_headers = [h for m, _, h in client.requests if m == "GET"]
        assert get_headers == [{"Range": "bytes=40-"}]
        assert not tmp_file.exists()

    @pytest.mark.parametrize("tmp_size", [10, 11], ids=["exact", "over"])
    def test_complete_tmp_short_circuits_network(
        self, tmp_path: Path, monkeypatch, tmp_size: int
    ) -> None:
        """已下部分 ≥ content_length → 直接落位，零网络 GET。"""
        client = _ScriptedClient(
            {
                ("HEAD", _URL): [_FakeResp(b"", {"content-length": "10"})],
                ("GET", _URL): [_FakeResp(b"SHOULD-NOT-BE-REQUESTED")],
            }
        )
        _install_client(monkeypatch, client)
        tmp_file = tmp_path / "cp.bin.tmp"
        tmp_file.write_bytes(b"t" * tmp_size)

        tool = DownloadTool()
        result = _run(
            tool._download(
                url=_URL,
                save_dir=tmp_path,
                filename="cp.bin",
                max_connections=1,
                max_retries=1,
                timeout=5,
                max_size=0,
                proxy=None,
            )
        )

        assert (tmp_path / "cp.bin").read_bytes() == b"t" * tmp_size
        assert result["size"] == 10
        assert result["path"] == tmp_path / "cp.bin"
        assert [r for r in client.requests if r[0] == "GET"] == []

    def test_stream_over_max_size_retries_then_succeeds(
        self, tmp_path: Path, monkeypatch, fake_sleep
    ) -> None:
        """HEAD 连续失败（触发 HEAD 退避重试）→ 回退流式；流式首试超限
        （触发流式退避重试）→ 二试成功。两处退避时钟均有界。"""
        client = _ScriptedClient(
            {
                ("HEAD", _URL_BIG): [httpx.ConnectError("head-boom")],
                ("GET", _URL_BIG): [_FakeResp(b"a" * 100), _FakeResp(b"ok!")],
            }
        )
        _install_client(monkeypatch, client)
        tool = DownloadTool()
        result = _run(
            tool._download(
                url=_URL_BIG,
                save_dir=tmp_path,
                filename=None,
                max_connections=1,
                max_retries=2,
                timeout=5,
                max_size=10,
                proxy=None,
            )
        )
        assert (tmp_path / "big.bin").read_bytes() == b"ok!"
        assert result["size"] == 3
        assert fake_sleep == [2, 2]
        assert all(0 < w <= 30 for w in fake_sleep)
        assert len([r for r in client.requests if r[0] == "HEAD"]) == 2
        assert len([r for r in client.requests if r[0] == "GET"]) == 2

    def test_stream_redirect_security_rejected_no_retry(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _patch_validate_reject(monkeypatch, "blocked.example")
        client = _ScriptedClient(
            {
                ("HEAD", _URL): [_FakeResp(b"", {"content-length": "0"})],
                ("GET", _URL): [
                    _FakeResp(b"", {"location": "http://blocked.example/evil"}, status_code=302)
                ],
            }
        )
        _install_client(monkeypatch, client)
        tool = DownloadTool()
        with pytest.raises(_MOD.RedirectSecurityError, match="重定向目标被拒绝"):
            _run(
                tool._download(
                    url=_URL,
                    save_dir=tmp_path,
                    filename="sr2.bin",
                    max_connections=1,
                    max_retries=3,
                    timeout=5,
                    max_size=0,
                    proxy=None,
                )
            )
        # 安全拒绝不重试
        assert len([r for r in client.requests if r[0] == "GET"]) == 1


# ═══════════════════════════════════════════════════════════
# 重定向链：缺 Location 头
# ═══════════════════════════════════════════════════════════


class TestFollowRedirectsNoLocation:
    @pytest.mark.parametrize("status", [302, 308], ids=["302", "308"])
    def test_redirect_without_location_returned_as_final(self, monkeypatch, status: int) -> None:
        """重定向响应缺 Location 头 → 原样返回，不追下一跳、不做多余校验。"""
        validated: list[str] = []

        def fake_validate(url: str, allow_domains: list[str] | None = None) -> tuple[bool, str]:
            validated.append(url)
            return True, "OK"

        monkeypatch.setattr(_MOD, "validate_url", fake_validate)
        resp = _FakeResp(b"", status_code=status)
        client = _ScriptedClient({("GET", _URL): [resp]})
        tool = DownloadTool()
        out = _run(tool._follow_redirects(client, "GET", _URL))
        assert out is resp
        assert len(client.requests) == 1
        assert validated == []


# ═══════════════════════════════════════════════════════════
# 公开 schema 安全契约
# ═══════════════════════════════════════════════════════════


class TestToolDefinitionContract:
    def test_public_schema_hides_ssrf_skip(self) -> None:
        """skip_ssrf_check 不在公开 schema：SSRF 旁路仅服务端构造位可控。"""
        definition = DownloadTool.get_tool_definition()
        assert definition.name == "download"
        props = definition.input_schema["properties"]
        assert "skip_ssrf_check" not in props
        assert definition.input_schema["required"] == ["url", "save_path"]
        assert definition.injected_params == ["skip_ssrf_check"]
        assert set(props) >= {"url", "save_path", "filename", "max_size", "proxy"}
