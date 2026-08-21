# @feature: FP-MIGR 0.1→0.2迁移（0.1 遗留测试） | @ci: python-coverage
"""channel_api server.py 纯助手函数单测（mypy 收紧批配套）。

意图（WHY）：
- 2026-08-21 治理批次收紧 _http_exc_response（status None 显式 500）、
  _parse_multipart（email.parser 返回值收窄 + 文件字段 bytes 判定）、
  tasks/thinking-mode 域处理器签名后，这些行进入 diff-coverage 度量面。
- 本文件进程内直测模块级助手 + 两条域处理器的 404/推荐分支，不拉起 FastAPI app。
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
from fastapi import HTTPException

from tests.channels.conftest import use_channel

use_channel("api")

import server as srv  # noqa: E402

pytestmark = pytest.mark.unit


def _status_of(resp: dict) -> int:
    return resp["data"]["status"]


class TestHttpExcResponse:
    def test_fastapi_exception_with_status(self) -> None:
        resp = srv._http_exc_response(HTTPException(status_code=404, detail="nf"))
        assert _status_of(resp) == 404

    def test_generic_exception_maps_500(self) -> None:
        resp = srv._http_exc_response(ValueError("boom"))
        assert _status_of(resp) == 500


class TestDecodeBody:
    def test_empty(self) -> None:
        assert srv._decode_body("") == {}

    def test_plain_json(self) -> None:
        assert srv._decode_body('{"a": 1}') == {"a": 1}

    def test_base64_json(self) -> None:
        encoded = base64.b64encode(b'{"b": 2}').decode()
        assert srv._decode_body(encoded) == {"b": 2}


class TestParseMultipart:
    def _body(self, boundary: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="textfield"\r\n\r\nhello\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file1"; filename="a.txt"\r\n'
            f"Content-Type: text/plain\r\n\r\nFILECONTENT\r\n"
            f"--{boundary}--\r\n"
        ).encode()

    def test_text_and_file_fields(self) -> None:
        boundary = "testboundary42"
        fields = srv._parse_multipart(
            f"multipart/form-data; boundary={boundary}", self._body(boundary)
        )
        assert fields["textfield"] == "hello"
        file_field = fields["file1"]
        assert file_field["filename"] == "a.txt"
        assert file_field["content_type"] == "text/plain"
        assert file_field["data"] == b"FILECONTENT"

    def test_non_multipart_returns_empty(self) -> None:
        assert srv._parse_multipart("application/json", b"{}") == {}


class TestTasksDomainHandler:
    def test_unknown_subpath_404(self) -> None:
        resp = asyncio.run(
            srv._handle_tasks_domain("/ext/channel_api/tasks/unknown-sub", "GET", "", {})
        )
        assert _status_of(resp) == 404


class TestThinkingModeDomainHandler:
    def test_recommendations_with_empty_body(self) -> None:
        """空 body → recs_body=None → rtm.recommendations(None) 合法（参数默认即 None）。"""
        resp = srv._handle_thinking_mode_domain(
            "/ext/channel_api/thinking-mode/recommendations", "POST", "", {}
        )
        assert _status_of(resp) == 200
        body = json.loads(base64.b64decode(resp["data"]["body"]).decode("utf-8"))
        assert isinstance(body, list)

    def test_non_thinking_mode_path_404(self) -> None:
        resp = srv._handle_thinking_mode_domain("/ext/channel_api/other", "GET", "", {})
        assert _status_of(resp) == 404
