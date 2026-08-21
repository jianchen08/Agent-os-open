# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""download 插件（通用文件下载工具）单元测试。

覆盖（对齐 plugins/shared/tools/download/tool.py）：
1. 纯函数：_sanitize_filename / _extract_filename_from_url / _extract_filename_from_headers / _format_size
2. execute 参数校验：缺 url/save_path、SSRF 拦截、目录创建失败、哈希校验
3. _download 分段下载（伪 httpx client，Range 请求 + 断点续传跳过已完成分片）
4. 本地 HTTP 服务器集成：流式下载 / 超限失败 / 断点续传（.tmp 续传）
5. _retry_request 重试

SSRF 防护：集成测试用 allow_ssrf_skip=True（服务端受信位，文档声明的测试用途），
其余走真实 validate_url 校验内网拒绝。
"""

from __future__ import annotations

import asyncio
import hashlib
import http.server
import importlib.util
import sys
import threading
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
    mod_name = "download_tool_test"
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
_sanitize_filename = _MOD._sanitize_filename
_extract_filename_from_url = _MOD._extract_filename_from_url
_extract_filename_from_headers = _MOD._extract_filename_from_headers
_format_size = _MOD._format_size


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ═══════════════════════════════════════════════════════════
# 纯函数
# ═══════════════════════════════════════════════════════════


class TestPureHelpers:
    def test_sanitize_filename(self) -> None:
        assert _sanitize_filename("../../etc/passwd") == "passwd"
        assert _sanitize_filename("a<b>c.txt") == "a_b_c.txt"
        assert _sanitize_filename("  报告 v1.2.txt  ") == "报告 v1.2.txt"
        assert _sanitize_filename("...") == "download"  # 全点 → 兜底名
        assert _sanitize_filename("") == "download"

    def test_extract_filename_from_url(self) -> None:
        assert _extract_filename_from_url("https://example.com/files/report.pdf") == "report.pdf"
        assert _extract_filename_from_url("https://example.com/%E6%8A%A5%E5%91%8A.txt") == "报告.txt"
        assert _extract_filename_from_url("https://example.com/") == "download"

    def test_extract_filename_from_headers(self) -> None:
        assert _extract_filename_from_headers(httpx.Headers({"content-disposition": 'attachment; filename="a b.pdf"'})) == "a b.pdf"
        assert _extract_filename_from_headers(httpx.Headers({"content-disposition": "attachment; filename*=UTF-8''%E6%8A%A5%E5%91%8A.pdf"})) == "报告.pdf"
        assert _extract_filename_from_headers(httpx.Headers({})) is None

    def test_format_size(self) -> None:
        assert _format_size(500) == "500.0 B"
        assert _format_size(2048) == "2.0 KB"
        assert _format_size(5 * 1024 * 1024) == "5.0 MB"
        assert _format_size(3 * 1024 ** 3) == "3.0 GB"
        assert _format_size(2 * 1024 ** 4) == "2.0 TB"
        assert _format_size(9 * 1024 ** 5) == "9.0 PB"


# ═══════════════════════════════════════════════════════════
# execute：参数校验与安全
# ═══════════════════════════════════════════════════════════


class TestExecuteValidation:
    def test_missing_url(self, tmp_path: Path) -> None:
        tool = DownloadTool()
        r = _run(tool.execute({"save_path": str(tmp_path)}))
        assert not r.success and "url" in r.error

    def test_missing_save_path(self) -> None:
        tool = DownloadTool()
        r = _run(tool.execute({"url": "https://example.com/a.txt"}))
        assert not r.success and "save_path" in r.error

    def test_ssrf_rejects_private_ip(self, tmp_path: Path) -> None:
        """内网地址（127.0.0.1）→ SSRF 拦截（无 allow_ssrf_skip 时不可旁路）。"""
        tool = DownloadTool()
        r = _run(tool.execute({"url": "http://127.0.0.1:8080/x.txt", "save_path": str(tmp_path)}))
        assert not r.success and "SSRF" in r.error or "安全校验" in r.error

    def test_ssrf_rejects_bad_protocol(self, tmp_path: Path) -> None:
        tool = DownloadTool()
        r = _run(tool.execute({"url": "ftp://example.com/x.txt", "save_path": str(tmp_path)}))
        assert not r.success and "协议" in r.error

    def test_allow_domains_whitelist(self, tmp_path: Path) -> None:
        tool = DownloadTool()
        r = _run(
            tool.execute(
                {"url": "http://example.com/x.txt", "save_path": str(tmp_path), "allow_domains": ["github.com"]}
            )
        )
        assert not r.success and "白名单" in r.error

    def test_save_dir_creation_failure(self, tmp_path: Path) -> None:
        """save_path 是文件路径 → mkdir 失败 → 创建目录失败。"""
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        tool = DownloadTool(allow_ssrf_skip=True)
        r = _run(tool.execute({"url": "http://127.0.0.1:1/a.txt", "save_path": str(blocker / "sub")}))
        assert not r.success and "创建保存目录失败" in r.error

    def test_hash_mismatch(self, tmp_path: Path) -> None:
        """下载成功但哈希不符 → 校验失败。"""
        tool = DownloadTool(allow_ssrf_skip=True)

        async def fake_download(**kwargs: Any) -> dict:
            f = kwargs["save_dir"] / "out.bin"
            f.write_bytes(b"hello")
            return {"path": f, "size": 5, "segments": 1, "resumed": False}

        tool._download = fake_download  # type: ignore[method-assign]
        r = _run(
            tool.execute(
                {
                    "url": "http://127.0.0.1:1/out.bin",
                    "save_path": str(tmp_path),
                    "expected_hash": "0" * 64,
                }
            )
        )
        assert not r.success and "哈希校验失败" in r.error

    def test_hash_match_and_success_result(self, tmp_path: Path) -> None:
        tool = DownloadTool(allow_ssrf_skip=True)
        real_hash = hashlib.sha256(b"hello").hexdigest()

        async def fake_download(**kwargs: Any) -> dict:
            f = kwargs["save_dir"] / "out.bin"
            f.write_bytes(b"hello")
            return {"path": f, "size": 5, "segments": 2, "resumed": True}

        tool._download = fake_download  # type: ignore[method-assign]
        r = _run(
            tool.execute(
                {
                    "url": "http://127.0.0.1:1/out.bin",
                    "save_path": str(tmp_path),
                    "expected_hash": real_hash,
                }
            )
        )
        assert r.success
        assert r.output["size"] == 5
        assert r.metadata["segments"] == 2
        assert r.metadata["resumed"] is True
        # fake 下载极快时 elapsed 可为 0（Windows 计时器分辨率），此时按设计为 "N/A"
        assert r.output["avg_speed"].endswith("/s") or r.output["avg_speed"] == "N/A"

    def test_download_exception_wrapped(self, tmp_path: Path) -> None:
        tool = DownloadTool(allow_ssrf_skip=True)

        async def fake_download(**kwargs: Any) -> dict:
            raise RuntimeError("network unreachable")

        tool._download = fake_download  # type: ignore[method-assign]
        r = _run(tool.execute({"url": "http://127.0.0.1:1/a.txt", "save_path": str(tmp_path)}))
        assert not r.success and "network unreachable" in r.error


# ═══════════════════════════════════════════════════════════
# _download：分段下载（伪 httpx client）
# ═══════════════════════════════════════════════════════════


class _FakeResp:
    def __init__(
        self,
        content: bytes,
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
        pass

    async def aiter_bytes(self, chunk_size: int) -> Any:
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i : i + chunk_size]


class _FakeAsyncClient:
    """支持 Range 的伪 httpx.AsyncClient。"""

    def __init__(self, content: bytes, head_headers: dict[str, str] | None = None) -> None:
        self._content = content
        self._head_headers = head_headers or {}
        self.get_calls: list[dict | None] = []

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        pass

    async def request(self, method: str, url: str, headers: dict | None = None, **kwargs: Any) -> _FakeResp:
        # _follow_redirects 经 client.request 发起；转发到既有 head/get 语义
        if method.upper() == "HEAD":
            return await self.head(url, **kwargs)
        return await self.get(url, headers=headers, **kwargs)

    async def head(self, url: str, **kwargs: Any) -> _FakeResp:
        return _FakeResp(b"", self._head_headers)

    async def get(self, url: str, headers: dict | None = None, **kwargs: Any) -> _FakeResp:
        self.get_calls.append(headers)
        rng = (headers or {}).get("Range")
        if rng:
            start_s, end_s = rng.split("=")[1].split("-")
            start, end = int(start_s), int(end_s)
            chunk = self._content[start : end + 1]
            return _FakeResp(chunk, {"content-length": str(len(chunk))})
        return _FakeResp(self._content, {"content-length": str(len(self._content))})


class TestSegmentedDownload:
    def _install_fake_client(self, monkeypatch, content: bytes) -> _FakeAsyncClient:
        client = _FakeAsyncClient(
            content,
            head_headers={
                "content-length": str(len(content)),
                "accept-ranges": "bytes",
                "etag": '"abc123"',
            },
        )
        monkeypatch.setattr(_MOD.httpx, "AsyncClient", lambda **kw: client)
        return client

    def test_segmented_download_multiple_segments(self, tmp_path: Path, monkeypatch) -> None:
        content = bytes(range(256)) * 64  # 16KB
        client = self._install_fake_client(monkeypatch, content)
        tool = DownloadTool()
        monkeypatch.setattr(_MOD, "DEFAULT_SEGMENT_SIZE", 128)  # 缩小分片阈值，强制多段

        result = _run(
            tool._download(
                url="http://example.com/data.bin",
                save_dir=tmp_path,
                filename=None,
                max_connections=4,
                max_retries=1,
                timeout=30,
                max_size=0,
                proxy=None,
            )
        )

        assert result["path"] == tmp_path / "data.bin"
        assert result["size"] == len(content)
        assert (tmp_path / "data.bin").read_bytes() == content
        assert result["segments"] == 4
        # 所有分片走 Range 请求
        assert all("Range" in (h or {}) for h in client.get_calls)
        # 状态文件已清理
        assert not (tmp_path / "data.bin.state.json").exists()

    def test_segmented_download_resume_skips_completed(self, tmp_path: Path, monkeypatch) -> None:
        """断点续传：已完成分片（etag 匹配 + part 文件完整）跳过重下。"""
        content = bytes(range(256)) * 64
        client = self._install_fake_client(monkeypatch, content)
        monkeypatch.setattr(_MOD, "DEFAULT_SEGMENT_SIZE", 128)
        # 构造续传状态：1 个已完成分片 + 对应 part 文件
        num_segments = 4
        seg_size = len(content) // num_segments
        state = {
            "etag": '"abc123"',
            "completed_segments": [0],
            "content_length": len(content),
        }
        (tmp_path / "data.bin.state.json").write_text(__import__("json").dumps(state), encoding="utf-8")
        (tmp_path / "data.bin.part.0").write_bytes(content[:seg_size])

        tool = DownloadTool()
        result = _run(
            tool._download(
                url="http://example.com/data.bin",
                save_dir=tmp_path,
                filename="data.bin",
                max_connections=4,
                max_retries=1,
                timeout=30,
                max_size=0,
                proxy=None,
            )
        )

        assert result["size"] == len(content)
        # 只重下了 3 个分片（part.0 跳过）
        range_calls = [h for h in client.get_calls if h]
        assert len(range_calls) == 3
        assert all("bytes=" in h["Range"] for h in range_calls)  # type: ignore[index]

    def test_size_exceeds_limit_raises(self, tmp_path: Path, monkeypatch) -> None:
        content = b"x" * 100
        self._install_fake_client(monkeypatch, content)
        tool = DownloadTool()
        with pytest.raises(ValueError, match="超过上限"):
            _run(
                tool._download(
                    url="http://example.com/big.bin",
                    save_dir=tmp_path,
                    filename=None,
                    max_connections=2,
                    max_retries=1,
                    timeout=30,
                    max_size=50,
                    proxy=None,
                )
            )

    def test_retry_request_success_and_failure(self, monkeypatch) -> None:
        tool = DownloadTool()

        class _FailingMethod:
            def __init__(self) -> None:
                self.calls = 0

            async def __call__(self, url: str, **kw: Any) -> _FakeResp:
                self.calls += 1
                if self.calls < 2:
                    raise httpx.ConnectError("refused")
                return _FakeResp(b"", {})

        method = _FailingMethod()
        resp = _run(tool._retry_request(method, "http://x/", max_retries=2))
        assert resp.status_code == 200
        assert method.calls == 2

        async def always_fail(url: str, **kw: Any) -> Any:
            raise httpx.ConnectError("refused")

        with pytest.raises(RuntimeError, match="请求失败"):
            _run(tool._retry_request(always_fail, "http://x/", max_retries=1))


# ═══════════════════════════════════════════════════════════
# 手动逐跳重定向（B3）：每跳 Location 经 validate_url 复检
# ═══════════════════════════════════════════════════════════


class _ScriptedClient:
    """按 (METHOD, url) 脚本化响应的伪 httpx.AsyncClient（重定向测试用）。"""

    def __init__(self, script: dict[tuple[str, str], _FakeResp]) -> None:
        self._script = script
        self.requests: list[tuple[str, str]] = []

    async def __aenter__(self) -> _ScriptedClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        pass

    async def request(self, method: str, url: str, **kwargs: Any) -> _FakeResp:
        self.requests.append((method.upper(), url))
        return self._script[(method.upper(), url)]


class TestRedirectSecurity:
    def test_redirect_to_private_ip_rejected(self, tmp_path: Path, monkeypatch) -> None:
        """302 → 127.0.0.1：重定向目标经 validate_url 复检被拒，不再请求下一跳。"""

        def fake_validate(url: str, allow_domains: list[str] | None = None) -> tuple[bool, str]:
            if "127.0.0.1" in url:
                return False, "域名 127.0.0.1 解析到内网 IP 127.0.0.1（SSRF 防护）"
            return True, "OK"

        monkeypatch.setattr(_MOD, "validate_url", fake_validate)
        client = _ScriptedClient(
            {
                ("HEAD", "http://example.com/f.bin"): _FakeResp(
                    b"", {"location": "http://127.0.0.1:8080/evil"}, status_code=302
                ),
            }
        )
        monkeypatch.setattr(_MOD.httpx, "AsyncClient", lambda **kw: client)

        tool = DownloadTool()
        with pytest.raises(_MOD.RedirectSecurityError, match="重定向目标被拒绝"):
            _run(
                tool._download(
                    url="http://example.com/f.bin",
                    save_dir=tmp_path,
                    filename=None,
                    max_connections=1,
                    max_retries=1,
                    timeout=5,
                    max_size=0,
                    proxy=None,
                )
            )
        # 只打到入口 URL，内网下一跳从未被请求
        assert client.requests == [("HEAD", "http://example.com/f.bin")]

    def test_redirect_relative_location_resolved_and_validated(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """相对 Location（/files/final.bin）归一为绝对地址并复检，通过后正常下载。"""
        validated: list[str] = []

        def fake_validate(url: str, allow_domains: list[str] | None = None) -> tuple[bool, str]:
            validated.append(url)
            return True, "OK"

        monkeypatch.setattr(_MOD, "validate_url", fake_validate)
        client = _ScriptedClient(
            {
                ("HEAD", "http://example.com/f.bin"): _FakeResp(
                    b"", {"location": "/files/final.bin"}, status_code=302
                ),
                ("HEAD", "http://example.com/files/final.bin"): _FakeResp(
                    b"", {"content-length": "5"}
                ),
                ("GET", "http://example.com/f.bin"): _FakeResp(
                    b"", {"location": "/files/final.bin"}, status_code=302
                ),
                ("GET", "http://example.com/files/final.bin"): _FakeResp(
                    b"hello", {"content-length": "5"}
                ),
            }
        )
        monkeypatch.setattr(_MOD.httpx, "AsyncClient", lambda **kw: client)

        tool = DownloadTool()
        result = _run(
            tool._download(
                url="http://example.com/f.bin",
                save_dir=tmp_path,
                filename=None,
                max_connections=1,
                max_retries=1,
                timeout=5,
                max_size=0,
                proxy=None,
            )
        )
        assert (tmp_path / "f.bin").read_bytes() == b"hello"
        assert result["size"] == 5
        # 相对 Location 已归一为绝对地址发起请求，且该地址经过了复检
        assert ("GET", "http://example.com/files/final.bin") in client.requests
        assert "http://example.com/files/final.bin" in validated

    def test_redirect_loop_capped(self, tmp_path: Path, monkeypatch) -> None:
        """无限重定向：受 MAX_REDIRECTS 上限约束，不悬挂。"""
        monkeypatch.setattr(_MOD, "validate_url", lambda url, allow_domains=None: (True, "OK"))
        loop_url = "http://example.com/loop"
        client = _ScriptedClient(
            {
                ("HEAD", loop_url): _FakeResp(b"", {"location": loop_url}, status_code=302),
                ("GET", loop_url): _FakeResp(b"", {"location": loop_url}, status_code=302),
            }
        )
        monkeypatch.setattr(_MOD.httpx, "AsyncClient", lambda **kw: client)

        tool = DownloadTool()
        with pytest.raises(RuntimeError):
            _run(
                tool._download(
                    url=loop_url,
                    save_dir=tmp_path,
                    filename=None,
                    max_connections=1,
                    max_retries=1,
                    timeout=5,
                    max_size=0,
                    proxy=None,
                )
            )
        # 每轮重定向链最多 MAX_REDIRECTS + 1 次请求，无失控
        assert len(client.requests) <= (_MOD.MAX_REDIRECTS + 1) * 4


# ═══════════════════════════════════════════════════════════
# 本地 HTTP 服务器集成：流式下载 / 超限 / 断点续传
# ═══════════════════════════════════════════════════════════


class _LocalServer:
    """在独立线程提供静态文件的本地 HTTP 服务器（支持 Range 请求）。"""

    def __init__(self, root: Path, files: dict[str, bytes]) -> None:
        for name, content in files.items():
            (root / name).write_bytes(content)
        files_map = dict(files)

        class Handler(http.server.BaseHTTPRequestHandler):
            # HTTP/1.0：关闭 keep-alive，规避 http.server 在
            # HEAD 请求后同一连接 GET 返回 502 的已知缺陷
            protocol_version = "HTTP/1.0"

            def log_message(self, *args: Any) -> None:
                pass

            def _serve(self) -> None:
                data = files_map.get(self.path.lstrip("/"))
                if data is None:
                    self.send_error(404)
                    return
                rng = self.headers.get("Range")
                if rng:
                    start_s, end_s = rng.split("=")[1].split("-")
                    start = int(start_s)
                    end = int(end_s) if end_s else len(data) - 1
                    body = data[start : end + 1]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{start + len(body) - 1}/{len(data)}")
                else:
                    body = data
                    self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_GET(self) -> None:
                self._serve()

            def do_HEAD(self) -> None:
                self._serve()

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _LocalServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)
        self._server.server_close()

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.port}/{name}"


class TestLocalServerIntegration:
    @pytest.fixture(autouse=True)
    def _no_system_proxy(self, monkeypatch) -> None:
        """本机系统代理（WinINET）会被 httpx 拾取，导致发往本地测试服务器的
        请求经代理转发返回 502。测试强制 trust_env=False 直连。"""
        real_client = _MOD.httpx.AsyncClient
        monkeypatch.setattr(
            _MOD.httpx, "AsyncClient", lambda **kw: real_client(**{**kw, "trust_env": False})
        )

    def test_stream_download_via_local_server(self, tmp_path: Path) -> None:
        payload = ("hello world\n" * 100).encode("utf-8")
        with _LocalServer(tmp_path, {"data.txt": payload}) as server:
            tool = DownloadTool(allow_ssrf_skip=True)  # 受信本地测试服务器
            save_dir = tmp_path / "out"
            r = _run(tool.execute({"url": server.url("data.txt"), "save_path": str(save_dir)}))
        assert r.success
        assert (save_dir / "data.txt").read_bytes() == payload
        assert r.output["size"] == len(payload)
        assert r.metadata["resumed"] is False

    def test_max_size_exceeded_fails(self, tmp_path: Path) -> None:
        payload = b"y" * 4096
        with _LocalServer(tmp_path, {"big.bin": payload}) as server:
            tool = DownloadTool(allow_ssrf_skip=True)
            save_dir = tmp_path / "out"
            r = _run(
                tool.execute(
                    {
                        "url": server.url("big.bin"),
                        "save_path": str(save_dir),
                        "max_size": 100,
                        "max_retries": 1,
                    }
                )
            )
        assert not r.success and "超过上限" in r.error

    def test_stream_resume_with_tmp_file(self, tmp_path: Path) -> None:
        """已存在 .tmp 部分文件 → 续传路径（resumed=True）。"""
        payload = b"z" * 2048
        with _LocalServer(tmp_path, {"f.bin": payload}) as server:
            tool = DownloadTool(allow_ssrf_skip=True)
            save_dir = tmp_path / "out"
            save_dir.mkdir()
            (save_dir / "f.bin.tmp").write_bytes(b"z" * 512)  # 部分下载
            r = _run(tool.execute({"url": server.url("f.bin"), "save_path": str(save_dir)}))
        assert r.success
        assert r.metadata["resumed"] is True
        assert (save_dir / "f.bin").stat().st_size == len(payload)
