# @feature: FP-0.2.二 可观测性 | @ci: python-coverage
"""multimodal_service 插件 audio + files 域测试（channel_api 自持承接）。

覆盖 /ext/multimodal_service/ 3 端点：
1. GET files/capabilities —— ModelCapabilityRegistry 真实能力（前端 files.ts 消费形态）
2. GET files/supported-types —— 静态宽类型声明
3. POST audio/transcriptions —— multipart 解析 + ASR 服务（503 未配置 / 400 空文件 /
   502 转写失败 / language 透传），响应形态对齐源 routes_asr.py
4. 404 未知路由 / 非 multipart / 非法 base64 边界
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

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "system" / "multimodal"


def _load_server() -> Any:
    """动态加载 multimodal/server.py（每次新建，隔离模块级状态）。"""
    spec = importlib.util.spec_from_file_location(
        "multimodal_http_test_server",
        str(_PLUGIN_DIR / "server.py"),
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["multimodal_http_test_server"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server() -> Any:
    return _load_server()


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _call(
    server: Any,
    path: str,
    method: str = "GET",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    return _run(server.http_handle(path=path, method=method, raw_body=raw_body,
                                   headers=headers, query=query))


def _decode(result: dict[str, Any]) -> tuple[int, Any]:
    """解包 http.handle 返回 → (status, json_body)。"""
    assert result["success"], result
    resp = result["data"]
    body = base64.b64decode(resp["body"]).decode("utf-8")
    return resp["status"], json.loads(body)


# ── multipart 构造（标准库 email 拼装，与生产 _parse_multipart 对称）─────────


def _multipart(fields: dict[str, Any]) -> tuple[str, str]:
    """构造 multipart/form-data 报文 → (content_type, base64_body)。

    fields: {name: (filename, content_type, bytes) | str}
    """
    import uuid
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    boundary = f"----testboundary{uuid.uuid4().hex}"
    msg = MIMEMultipart("form-data", boundary=boundary)
    for name, value in fields.items():
        if isinstance(value, tuple):
            filename, file_ct, data = value
            part = MIMEText("", "plain")
            part.set_payload(data, charset=None)
            del part["content-type"]
            part["Content-Type"] = file_ct
            part["Content-Disposition"] = f'form-data; name="{name}"; filename="{filename}"'
            part.set_payload(data)
        else:
            part = MIMEText(str(value), "plain", "utf-8")
            part["Content-Disposition"] = f'form-data; name="{name}"'
        msg.attach(part)
    body = msg.as_bytes()
    content_type = f"multipart/form-data; boundary={boundary}"
    return content_type, base64.b64encode(body).decode("ascii")


# ── files 域 ──────────────────────────────────────────────────────────────


class _FakeCap:
    """对齐 ModelCapability 字段的假能力对象。"""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.supports_image = True
        self.supports_audio = False
        self.supports_video = True
        self.supported_image_types = ["image/png", "image/jpeg"]
        self.supported_audio_types: list[str] = []
        self.supported_video_types = ["video/mp4"]
        self.max_image_size = 10 * 1024 * 1024
        self.max_audio_size = 0
        self.max_video_size = 50 * 1024 * 1024


def test_files_capabilities_default_model(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server.ModelCapabilityRegistry, "get_capability",
                        lambda name: _FakeCap(name))

    status, body = _decode(_call(server, "/ext/multimodal_service/files/capabilities"))

    assert status == 200
    assert body["model_name"] == "default"
    assert body["supports_image"] is True
    assert body["supports_audio"] is False
    assert body["supports_video"] is True
    assert body["supported_image_types"] == ["image/png", "image/jpeg"]
    assert body["max_image_size"] == 10 * 1024 * 1024
    assert body["is_multimodal"] is True  # image or video → multimodal


def test_files_capabilities_model_name_query(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(server.ModelCapabilityRegistry, "get_capability",
                        lambda name: (seen.append(name), _FakeCap(name))[1])

    status, body = _decode(_call(
        server, "/ext/multimodal_service/files/capabilities", query={"model_name": "glm-5.2"}
    ))

    assert status == 200
    assert body["model_name"] == "glm-5.2"
    assert seen == ["glm-5.2"]


def test_files_supported_types(server: Any) -> None:
    status, body = _decode(_call(server, "/ext/multimodal_service/files/supported-types"))

    assert status == 200
    assert body["image_types"]["default"] == ["image/png", "image/jpeg", "image/gif", "image/webp"]
    assert body["document_types"]["default"] == [
        "application/pdf", "text/plain", "text/markdown", "text/csv"
    ]
    assert body["max_image_size"] == 20 * 1024 * 1024
    assert body["max_document_size"] == 50 * 1024 * 1024


# ── audio/transcriptions ──────────────────────────────────────────────────


class _FakeASR:
    def __init__(self, available: bool = True, text: str = "转写结果") -> None:
        self._available = available
        self._text = text

    def is_available(self) -> bool:
        return self._available

    async def transcribe(self, audio_bytes: bytes, mime_type: str, language: str | None) -> str:
        return self._text


def test_transcribe_success(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import asr as asr_mod

    monkeypatch.setattr(asr_mod, "get_asr_service", lambda: _FakeASR(text="hello world"))
    content_type, body = _multipart({"file": ("audio.webm", "audio/webm", b"\x1aE\xdf\xa3fake")})

    status, body_resp = _decode(_call(
        server, "/ext/multimodal_service/audio/transcriptions", "POST",
        raw_body=body, headers={"Content-Type": content_type},
    ))

    assert status == 200
    assert body_resp == {"text": "hello world"}


def test_transcribe_passes_language(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import asr as asr_mod

    captured: dict[str, Any] = {}

    class _Recorder(_FakeASR):
        async def transcribe(self, audio_bytes: bytes, mime_type: str, language: str | None) -> str:
            captured.update(audio=audio_bytes, mime=mime_type, lang=language)
            return "ok"

    monkeypatch.setattr(asr_mod, "get_asr_service", lambda: _Recorder())
    content_type, body = _multipart({
        "file": ("a.mp3", "audio/mpeg", b"mp3data"),
        "language": "en-US",
    })

    status, _ = _decode(_call(
        server, "/ext/multimodal_service/audio/transcriptions", "POST",
        raw_body=body, headers={"Content-Type": content_type},
    ))

    assert status == 200
    assert captured["audio"] == b"mp3data"
    assert captured["mime"] == "audio/mpeg"
    assert captured["lang"] == "en-US"


def test_transcribe_not_configured_503(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import asr as asr_mod

    monkeypatch.setattr(asr_mod, "get_asr_service", lambda: _FakeASR(available=False))
    content_type, body = _multipart({"file": ("a.webm", "audio/webm", b"data")})

    status, body_resp = _decode(_call(
        server, "/ext/multimodal_service/audio/transcriptions", "POST",
        raw_body=body, headers={"Content-Type": content_type},
    ))

    assert status == 503
    assert body_resp["code"] == "asr_not_configured"


def test_transcribe_failure_502(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import asr as asr_mod

    class _Boom(_FakeASR):
        async def transcribe(self, audio_bytes: bytes, mime_type: str, language: str | None) -> str:
            raise RuntimeError("upstream timeout")

    monkeypatch.setattr(asr_mod, "get_asr_service", lambda: _Boom())
    content_type, body = _multipart({"file": ("a.webm", "audio/webm", b"data")})

    status, body_resp = _decode(_call(
        server, "/ext/multimodal_service/audio/transcriptions", "POST",
        raw_body=body, headers={"Content-Type": content_type},
    ))

    assert status == 502
    assert body_resp["code"] == "asr_failed"


def test_transcribe_missing_file_400(server: Any) -> None:
    content_type, body = _multipart({"language": "zh-CN"})

    status, body_resp = _decode(_call(
        server, "/ext/multimodal_service/audio/transcriptions", "POST",
        raw_body=body, headers={"Content-Type": content_type},
    ))

    assert status == 400
    assert "file" in body_resp["error"]["message"]


def test_transcribe_non_multipart_400(server: Any) -> None:
    status, _ = _decode(_call(
        server, "/ext/multimodal_service/audio/transcriptions", "POST",
        raw_body=base64.b64encode(b"{}").decode(), headers={"Content-Type": "application/json"},
    ))

    assert status == 400


def test_transcribe_invalid_base64_400(server: Any) -> None:
    status, _ = _decode(_call(
        server, "/ext/multimodal_service/audio/transcriptions", "POST",
        raw_body="!!!not-base64!!!", headers={"Content-Type": "multipart/form-data; boundary=x"},
    ))

    assert status == 400


# ── 分发层边界 ────────────────────────────────────────────────────────────


def test_unknown_route_404(server: Any) -> None:
    status, body = _decode(_call(server, "/ext/multimodal_service/nope"))

    assert status == 404
    assert "not found" in body["error"]["message"]


# ── 补充分支覆盖（diff coverage 收口）────────────────────────────────────


def test_transcribe_generic_exception_500(server: Any) -> None:
    import asr as asr_mod

    class _Boom2(_FakeASR):
        async def transcribe(self, audio_bytes: bytes, mime_type: str, language: str | None) -> str:
            raise ValueError("unexpected")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(asr_mod, "get_asr_service", lambda: _Boom2())
    content_type, body = _multipart({"file": ("a.webm", "audio/webm", b"data")})
    try:
        status, body_resp = _decode(_call(
            server, "/ext/multimodal_service/audio/transcriptions", "POST",
            raw_body=body, headers={"Content-Type": content_type},
        ))
    finally:
        monkeypatch.undo()

    assert status == 500


def test_transcribe_empty_language_normalized(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import asr as asr_mod

    captured: dict[str, Any] = {}

    class _Recorder2(_FakeASR):
        async def transcribe(self, audio_bytes: bytes, mime_type: str, language: str | None) -> str:
            captured["lang"] = language
            return "ok"

    monkeypatch.setattr(asr_mod, "get_asr_service", lambda: _Recorder2())
    content_type, body = _multipart({"file": ("a.webm", "audio/webm", b"d"), "language": ""})

    status, _ = _decode(_call(
        server, "/ext/multimodal_service/audio/transcriptions", "POST",
        raw_body=body, headers={"Content-Type": content_type},
    ))

    assert status == 200
    assert captured["lang"] is None  # 空串 language → None（默认配置）


def test_transcribe_without_content_type_header_400(server: Any) -> None:
    """headers 缺失 content-type → 400（requires multipart，头循环穷尽分支）。"""
    _, body_b64 = _multipart({"file": ("a.webm", "audio/webm", b"d")})

    status, _ = _decode(_call(
        server, "/ext/multimodal_service/audio/transcriptions", "POST", raw_body=body_b64,
    ))

    assert status == 400


def test_transcribe_non_multipart_body_with_multipart_header_400(server: Any) -> None:
    """声明 multipart 但 body 不是 multipart 报文 → email 解析非 multipart → 缺 file 400。"""
    status, _ = _decode(_call(
        server, "/ext/multimodal_service/audio/transcriptions", "POST",
        raw_body=base64.b64encode(b"plain text body").decode(),
        headers={"Content-Type": "multipart/form-data; boundary=xyz"},
    ))

    assert status == 400


def test_transcribe_part_without_name_skipped(server: Any) -> None:
    """multipart 内含无 Content-Disposition 名段的 part → 解析跳过 → 缺 file 400。"""
    boundary = "bndr"
    raw = (
        f"--{boundary}\r\n"
        "Content-Type: text/plain\r\n"
        "\r\n"
        "orphan part without name\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    status, _ = _decode(_call(
        server, "/ext/multimodal_service/audio/transcriptions", "POST",
        raw_body=base64.b64encode(raw).decode(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    ))

    assert status == 400