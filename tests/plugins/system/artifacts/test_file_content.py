# @feature: FP-0.2.二 artifacts 插件(文件内容读面) | @vision: V3 可嵌入 | @ci: python-coverage
# @ci: python-coverage
"""artifacts 插件上传文件内容端点测试（文件加载器路由设计 §6.1）。

GET /ext/artifacts/files/{stored_filename} —— 加载器路由统一取流通道：
1. happy：上传后按落盘名取回字节（Content-Type 按 mimetypes）；
2. 缺文件 → 404；非法文件名（路径穿越/子路径）→ 400；
3. 非 GET → 404 无路由。

外部依赖经 env（UPLOADS_DIR / MULTIMODAL_STORAGE_DIR → tmp）隔离，
不落仓库 data/，不接真实内核（与 test_artifacts_http 同款夹具）。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "system" / "artifacts"

_BOUNDARY = "X-TEST-BOUNDARY-42"


def _load_server() -> Any:
    spec = importlib.util.spec_from_file_location(
        "artifacts_server_filecontent_test",
        str(_PLUGIN_DIR / "server.py"),
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["artifacts_server_filecontent_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server() -> Any:
    return _load_server()


@pytest.fixture
def storage_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    uploads = tmp_path / "uploads"
    monkeypatch.setenv("UPLOADS_DIR", str(uploads))
    monkeypatch.setenv("MULTIMODAL_STORAGE_DIR", str(tmp_path / "multimodal"))
    return str(uploads)


def _multipart(
    filename: str = "a.txt",
    content: bytes = b"hello attachment",
    content_type: str = "text/plain",
) -> tuple[str, str]:
    parts = [
        f'--{_BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n".encode()
        + content
        + b"\r\n",
        f"--{_BOUNDARY}--\r\n".encode(),
    ]
    raw = base64.b64encode(b"".join(parts)).decode()
    return raw, f"multipart/form-data; boundary={_BOUNDARY}"


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _call(server: Any, **kwargs: Any) -> dict[str, Any]:
    return _run(server.http_handle(**kwargs))


def _upload(server: Any) -> str:
    """上传 a.txt → 落盘名（/uploads url basename = {file_id}.txt）。"""
    raw, content_type = _multipart()
    result = _call(
        server,
        path="/ext/artifacts/upload",
        method="POST",
        raw_body=raw,
        headers={"content-type": content_type},
    )
    assert result["success"], result
    body = json.loads(base64.b64decode(result["data"]["body"]))
    return body["url"].rsplit("/", 1)[-1]


class TestUploadFileContent:
    def test_happy_path_roundtrip(
        self, server: Any, storage_dirs: str
    ) -> None:
        stored = _upload(server)
        result = _call(
            server,
            path=f"/ext/artifacts/files/{stored}",
            method="GET",
        )
        assert result["success"], result
        resp = result["data"]
        assert resp["status"] == 200
        assert resp["headers"]["Content-Type"] == "text/plain"
        assert base64.b64decode(resp["body"]) == b"hello attachment"

    def test_missing_file_404(self, server: Any, storage_dirs: str) -> None:
        result = _call(
            server,
            path="/ext/artifacts/files/deadbeef0000.txt",
            method="GET",
        )
        assert result["success"], result
        resp = result["data"]
        assert resp["status"] == 404

    def test_path_traversal_rejected(self, server: Any, storage_dirs: str) -> None:
        result = _call(
            server,
            path="/ext/artifacts/files/../secret.txt",
            method="GET",
        )
        resp = result["data"]
        assert resp["status"] == 400

    def test_subpath_rejected(self, server: Any, storage_dirs: str) -> None:
        result = _call(
            server,
            path="/ext/artifacts/files/sub/dir.txt",
            method="GET",
        )
        resp = result["data"]
        assert resp["status"] == 400

    def test_non_get_404(self, server: Any, storage_dirs: str) -> None:
        result = _call(
            server,
            path="/ext/artifacts/files/whatever.txt",
            method="POST",
            raw_body="",
        )
        resp = result["data"]
        assert resp["status"] == 404
