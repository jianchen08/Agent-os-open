# @feature: FP-0.2.review P1-2 http 面 | @vision: V1 可进化 | @ci: python-coverage
"""review 插件 http.handle 9 端点测试（channel_api 退役批次 5 P1-2 接真）。

覆盖 /ext/review_service/reviews/** 全 9 路由：
1. POST/GET /reviews —— 创建/列表（query: task_id/limit）
2. GET /reviews/{review_id} —— 详情（不存在 NOT_FOUND）
3. POST /reviews/{review_id}/feedback|viewed|cancel —— 状态流转 + 拒绝语义
4. POST /reviews/media-review —— multipart 上传审阅（真实 multipart 字节 +
   假媒体服务；临时文件落盘/清理）
5. GET /reviews/{review_id}/media-metadata —— 已存结果/实时解析/降级
6. POST /reviews/{review_id}/attachments —— JSON 附件 + auto_review
7. 协议级：非 multipart/缺 file/坏 base64/multipart 解析失败 → 400；
   未知路由/方法不匹配 → 404；handler 未预期异常 → 500

媒体处理按任务要求 mock（假 MediaReviewService 注入 server 模块单例；
PIL/PyAV 不落地——仅当 id 字段为 IS_PIL 时用真实路径，见 media-review 用例）。
认证由 plugin.json auth=user 声明承接，handler 不读 _user。
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

import review_service
from models import ImageReviewResult, VideoReviewResult

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "system" / "review"


def _load_server() -> Any:
    """动态加载 review/server.py（每次新建，隔离模块级状态）。"""
    spec = importlib.util.spec_from_file_location(
        "review_server_http_test",
        str(_PLUGIN_DIR / "server.py"),
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["review_server_http_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server() -> Any:
    return _load_server()


@pytest.fixture(autouse=True)
def _clean_singletons(server: Any) -> None:
    """每个测试后清空审批单例与媒体服务单例（防跨测试状态泄漏）。"""
    yield
    review_service.reset_review_service()
    server._media_review_service = None


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(coro)

        async def _cleanup() -> None:
            current = asyncio.current_task()
            pending = [
                t for t in asyncio.all_tasks(loop)
                if not t.done() and t is not current
            ]
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.sleep(0)

        loop.run_until_complete(_cleanup())
        return result
    finally:
        loop.close()


def _call(server: Any, **kwargs: Any) -> dict[str, Any]:
    """同步调用 http.handle（测试侧统一 asyncio 跑）。"""
    return _run(server.http_handle(**kwargs))


def _decode(result: dict[str, Any]) -> tuple[int, Any]:
    """解包 http.handle 返回 → (status, json_body)。"""
    assert result["success"], result
    resp = result["data"]
    body = base64.b64decode(resp["body"]).decode("utf-8")
    return resp["status"], json.loads(body)


def _body(payload: dict[str, Any]) -> str:
    """dict → base64 JSON raw_body。"""
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def _make_multipart(file_bytes: bytes, filename: str, media_type: str = "") -> tuple[str, str]:
    """构造 multipart/form-data 字节 → (raw_body base64, content_type)。

    普通字段 media_type 可选；文件字段名为 file。
    """
    boundary = "----reviewtestboundary42"
    parts = []
    if media_type:
        parts.append(
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="media_type"\r\n\r\n'
                f"{media_type}\r\n"
            ).encode()
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + file_bytes
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    return base64.b64encode(body).decode("ascii"), f"multipart/form-data; boundary={boundary}"


def _create(server: Any, **overrides: Any) -> str:
    """创建审批并返回 review_id（成功路径）。"""
    payload = {
        "task_id": "task-1",
        "thread_id": "thread-1",
        "session_id": "session-1",
        "tab_id": "tab-1",
        "title": "审批标题",
    }
    payload.update(overrides)
    status, body = _decode(_call(server, path="/ext/review_service/reviews", method="POST", raw_body=_body(payload)))
    assert status == 200
    assert "error" not in body, body
    return body["id"]


class _FakeMediaService:
    """假媒体审阅服务：review_media 动态返回，get_media_metadata 动态返回。

    用于验证 http 层的 multipart 解析/临时文件/错误映射，不落地 PIL/PyAV。
    """

    def __init__(self) -> None:
        self.review_media_result: Any = ImageReviewResult(is_valid=True, format="PNG", width=8, height=6)
        self.review_media_error: Exception | None = None
        self.called_with: list[tuple[str, str]] = []
        self.metadata_result: dict[str, Any] = {}
        self.metadata_error: Exception | None = None

    async def review_media(self, file_path: str, media_type: str) -> Any:
        self.called_with.append((file_path, media_type))
        if self.review_media_error is not None:
            raise self.review_media_error
        return self.review_media_result

    def get_media_metadata(self, file_path: str, media_type: str) -> dict[str, Any]:
        if self.metadata_error is not None:
            raise self.metadata_error
        return {
            "file_path": file_path,
            "media_type": media_type,
            **self.metadata_result,
        }


def _inject_media_service(server: Any, fake: _FakeMediaService) -> None:
    """把假媒体服务注入 server 模块单例（get_media_review_service 返回它）。"""
    server._media_review_service = fake


# ---------------------------------------------------------------------------
# POST /reviews（创建）
# ---------------------------------------------------------------------------


class TestCreateEndpoint:
    def test_create_minimal(self, server: Any) -> None:
        status, body = _decode(
            _call(
                server,
                path="/ext/review_service/reviews",
                method="POST",
                raw_body=_body({"task_id": "t1", "thread_id": "th", "session_id": "s", "tab_id": "tab", "title": "T"}),
            )
        )
        assert status == 200
        assert body["task_id"] == "t1"
        assert body["title"] == "T"
        assert body["status"] == "pending"
        assert body["artifact_ids"] == []
        assert body["priority"] == "normal"
        assert body["timeout_seconds"] == 86400.0

    def test_create_full(self, server: Any) -> None:
        status, body = _decode(
            _call(
                server,
                path="/ext/review_service/reviews",
                method="POST",
                raw_body=_body(
                    {
                        "task_id": "t1",
                        "thread_id": "th",
                        "session_id": "s",
                        "tab_id": "tab",
                        "title": "T",
                        "description": "d",
                        "artifact_ids": ["a1"],
                        "priority": "high",
                        "timeout_seconds": 30,
                        "metadata": {"k": "v"},
                    }
                ),
            )
        )
        assert status == 200
        assert body["description"] == "d"
        assert body["artifact_ids"] == ["a1"]
        assert body["timeout_seconds"] == 30
        assert body["metadata"] == {"k": "v"}

    def test_create_invalid_json_500(self, server: Any) -> None:
        status, body = _decode(
            _call(server, path="/ext/review_service/reviews", method="POST", raw_body="not-base64-and-not-json")
        )
        assert status == 500
        assert "internal server error" in body["error"]


# ---------------------------------------------------------------------------
# GET /reviews（列表）
# ---------------------------------------------------------------------------


class TestListEndpoint:
    def test_list_empty_without_task_id(self, server: Any) -> None:
        status, body = _decode(_call(server, path="/ext/review_service/reviews", method="GET"))
        assert (status, body) == (200, {"items": [], "total": 0})

    def test_list_by_task(self, server: Any) -> None:
        _create(server, task_id="t1", title="a")
        _create(server, task_id="t1", title="b")
        _create(server, task_id="t2", title="c")

        status, body = _decode(
            _call(server, path="/ext/review_service/reviews", method="GET", query={"task_id": "t1"})
        )
        assert status == 200
        assert body["total"] == 2
        assert {i["title"] for i in body["items"]} == {"a", "b"}

    def test_list_limit_query(self, server: Any) -> None:
        for i in range(3):
            _create(server, task_id="t1", title=f"r{i}")
        status, body = _decode(
            _call(server, path="/ext/review_service/reviews", method="GET", query={"task_id": "t1", "limit": "1"})
        )
        assert body["total"] == 1
        assert len(body["items"]) == 1

    def test_list_invalid_limit_falls_back(self, server: Any) -> None:
        _create(server, task_id="t1")
        status, body = _decode(
            _call(server, path="/ext/review_service/reviews", method="GET", query={"task_id": "t1", "limit": "abc"})
        )
        assert status == 200
        assert body["total"] == 1


# ---------------------------------------------------------------------------
# GET /reviews/{review_id}（详情）
# ---------------------------------------------------------------------------


class TestGetEndpoint:
    def test_get_found(self, server: Any) -> None:
        rid = _create(server, title="详情")
        status, body = _decode(_call(server, path=f"/ext/review_service/reviews/{rid}", method="GET"))
        assert status == 200
        assert body["id"] == rid
        assert body["title"] == "详情"

    def test_get_missing_not_found(self, server: Any) -> None:
        status, body = _decode(_call(server, path="/ext/review_service/reviews/nope", method="GET"))
        assert status == 200
        assert body["error"]["code"] == "NOT_FOUND"
        assert "nope" in body["error"]["message"]


# ---------------------------------------------------------------------------
# POST /reviews/{review_id}/feedback
# ---------------------------------------------------------------------------


class TestFeedbackEndpoint:
    def test_feedback_approved(self, server: Any) -> None:
        rid = _create(server)
        status, body = _decode(
            _call(
                server,
                path=f"/ext/review_service/reviews/{rid}/feedback",
                method="POST",
                raw_body=_body({"response_type": "approved", "overall_comment": "ok", "user_id": "u1"}),
            )
        )
        assert status == 200
        assert body["response_type"] == "approved"
        assert body["overall_comment"] == "ok"
        assert body["user_id"] == "u1"

        _, detail = _decode(_call(server, path=f"/ext/review_service/reviews/{rid}", method="GET"))
        assert detail["status"] == "approved"

    def test_feedback_annotations(self, server: Any) -> None:
        rid = _create(server)
        annotations = [{"artifact_id": "a1", "target_type": "text", "content": "x"}]
        status, body = _decode(
            _call(
                server,
                path=f"/ext/review_service/reviews/{rid}/feedback",
                method="POST",
                raw_body=_body({"response_type": "denied", "annotations": annotations}),
            )
        )
        assert body["annotations"] == annotations

    def test_feedback_missing_review_invalid(self, server: Any) -> None:
        status, body = _decode(
            _call(
                server,
                path="/ext/review_service/reviews/nope/feedback",
                method="POST",
                raw_body=_body({"response_type": "approved"}),
            )
        )
        assert body["error"]["code"] == "INVALID"

    def test_feedback_on_terminal_state_invalid(self, server: Any) -> None:
        rid = _create(server)
        _call(
            server,
            path=f"/ext/review_service/reviews/{rid}/feedback",
            method="POST",
            raw_body=_body({"response_type": "approved"}),
        )
        status, body = _decode(
            _call(
                server,
                path=f"/ext/review_service/reviews/{rid}/feedback",
                method="POST",
                raw_body=_body({"response_type": "denied"}),
            )
        )
        assert body["error"]["code"] == "INVALID"


# ---------------------------------------------------------------------------
# POST /reviews/{review_id}/viewed
# ---------------------------------------------------------------------------


class TestViewedEndpoint:
    def test_viewed_success(self, server: Any) -> None:
        rid = _create(server)
        status, body = _decode(_call(server, path=f"/ext/review_service/reviews/{rid}/viewed", method="POST"))
        assert (status, body) == (200, {"id": rid, "viewed": True})

        _, detail = _decode(_call(server, path=f"/ext/review_service/reviews/{rid}", method="GET"))
        assert detail["status"] == "in_review"
        assert detail["reviewed_at"]

    def test_viewed_missing_false(self, server: Any) -> None:
        status, body = _decode(_call(server, path="/ext/review_service/reviews/nope/viewed", method="POST"))
        assert body == {"id": "nope", "viewed": False}

    def test_viewed_twice_second_false(self, server: Any) -> None:
        rid = _create(server)
        _call(server, path=f"/ext/review_service/reviews/{rid}/viewed", method="POST")
        _, body = _decode(_call(server, path=f"/ext/review_service/reviews/{rid}/viewed", method="POST"))
        assert body["viewed"] is False


# ---------------------------------------------------------------------------
# POST /reviews/{review_id}/cancel
# ---------------------------------------------------------------------------


class TestCancelEndpoint:
    def test_cancel_with_reason(self, server: Any) -> None:
        rid = _create(server)
        status, body = _decode(
            _call(
                server,
                path=f"/ext/review_service/reviews/{rid}/cancel",
                method="POST",
                raw_body=_body({"reason": "不需要了"}),
            )
        )
        assert (status, body) == (200, {"id": rid, "cancelled": True})

        _, detail = _decode(_call(server, path=f"/ext/review_service/reviews/{rid}", method="GET"))
        assert detail["status"] == "cancelled"
        assert detail["metadata"]["cancel_reason"] == "不需要了"

    def test_cancel_without_body(self, server: Any) -> None:
        rid = _create(server)
        status, body = _decode(_call(server, path=f"/ext/review_service/reviews/{rid}/cancel", method="POST"))
        assert body == {"id": rid, "cancelled": True}

    def test_cancel_missing_false(self, server: Any) -> None:
        _, body = _decode(_call(server, path="/ext/review_service/reviews/nope/cancel", method="POST"))
        assert body == {"id": "nope", "cancelled": False}

    def test_cancel_approved_false(self, server: Any) -> None:
        rid = _create(server)
        _call(
            server,
            path=f"/ext/review_service/reviews/{rid}/feedback",
            method="POST",
            raw_body=_body({"response_type": "approved"}),
        )
        _, body = _decode(_call(server, path=f"/ext/review_service/reviews/{rid}/cancel", method="POST"))
        assert body == {"id": rid, "cancelled": False}


# ---------------------------------------------------------------------------
# POST /reviews/media-review（multipart）
# ---------------------------------------------------------------------------


class TestMediaReviewEndpoint:
    def test_media_review_success_with_explicit_type(self, server: Any, tmp_path: Any) -> None:
        fake = _FakeMediaService()
        _inject_media_service(server, fake)

        raw, ct = _make_multipart(b"PNGDATA", "a.png", media_type="image")
        status, body = _decode(
            _call(
                server,
                path="/ext/review_service/reviews/media-review",
                method="POST",
                raw_body=raw,
                headers={"Content-Type": ct},
            )
        )
        assert status == 200
        assert body["media_type"] == "image"
        assert body["filename"] == "a.png"
        assert body["is_valid"] is True
        assert fake.called_with[0][1] == "image"

    def test_media_review_infers_type(self, server: Any, tmp_path: Any) -> None:
        fake = _FakeMediaService()
        _inject_media_service(server, fake)

        raw, ct = _make_multipart(b"PNGDATA", "photo.jpg")
        _, body = _decode(
            _call(server, path="/ext/review_service/reviews/media-review", method="POST", raw_body=raw, headers={"Content-Type": ct})
        )
        assert body["media_type"] == "image"
        assert fake.called_with[0][1] == "image"

    def test_media_review_video_result(self, server: Any, tmp_path: Any) -> None:
        fake = _FakeMediaService()
        fake.review_media_result = VideoReviewResult(is_valid=True, format="MP4", duration_seconds=3.0)
        _inject_media_service(server, fake)

        raw, ct = _make_multipart(b"MP4DATA", "clip.mp4", media_type="video")
        _, body = _decode(
            _call(server, path="/ext/review_service/reviews/media-review", method="POST", raw_body=raw, headers={"Content-Type": ct})
        )
        assert body["media_type"] == "video"
        assert body["format"] == "MP4"
        assert body["duration_seconds"] == 3.0

    def test_media_review_written_file_and_cleaned(self, server: Any, tmp_path: Any) -> None:
        """临时文件落盘路径可被读取，且用毕即清（tmp dir 无残留）。"""
        import tempfile as _tf

        fake = _FakeMediaService()
        _inject_media_service(server, fake)

        # 固定 mkdtemp 落到 tmp_path 下便于断言残留
        real_mkdtemp = _tf.mkdtemp
        probe_dir: list[str] = []

        def _mkdtemp(*args: Any, **kwargs: Any) -> str:
            d = str(tmp_path / "probe")
            import os

            os.makedirs(d, exist_ok=True)
            probe_dir.append(d)
            return d

        _tf.mkdtemp = _mkdtemp  # type: ignore[assignment]
        try:
            raw, ct = _make_multipart(b"PNGDATA", "a.png", media_type="image")
            _, body = _decode(
                _call(server, path="/ext/review_service/reviews/media-review", method="POST", raw_body=raw, headers={"Content-Type": ct})
            )
        finally:
            _tf.mkdtemp = real_mkdtemp  # type: ignore[assignment]

        written_path, media_type = fake.called_with[0]
        assert media_type == "image"
        assert written_path.startswith(str(tmp_path / "probe"))
        # 文件在审阅时真实落盘（内容一致），结束后清理
        assert body["filename"] == "a.png"

        import os

        assert not os.path.exists(written_path)
        assert not os.path.isdir(probe_dir[0])

    def test_media_review_unknown_ext_invalid(self, server: Any, tmp_path: Any) -> None:
        fake = _FakeMediaService()
        _inject_media_service(server, fake)

        raw, ct = _make_multipart(b"TEXT", "notes.txt")
        _, body = _decode(
            _call(server, path="/ext/review_service/reviews/media-review", method="POST", raw_body=raw, headers={"Content-Type": ct})
        )
        assert body["error"]["code"] == "INVALID"
        assert "无法推断媒体类型" in body["error"]["message"]
        assert fake.called_with == []  # 审阅未被调用

    def test_media_review_file_not_found(self, server: Any, tmp_path: Any) -> None:
        fake = _FakeMediaService()
        fake.review_media_error = FileNotFoundError("文件不存在: x")
        _inject_media_service(server, fake)

        raw, ct = _make_multipart(b"DATA", "a.png", media_type="image")
        _, body = _decode(
            _call(server, path="/ext/review_service/reviews/media-review", method="POST", raw_body=raw, headers={"Content-Type": ct})
        )
        assert body["error"]["code"] == "NOT_FOUND"

    def test_media_review_invalid_value_error(self, server: Any, tmp_path: Any) -> None:
        fake = _FakeMediaService()
        fake.review_media_error = ValueError("不支持的媒体类型: audio")
        _inject_media_service(server, fake)

        raw, ct = _make_multipart(b"DATA", "a.png", media_type="audio")
        _, body = _decode(
            _call(server, path="/ext/review_service/reviews/media-review", method="POST", raw_body=raw, headers={"Content-Type": ct})
        )
        assert body["error"]["code"] == "INVALID"

    def test_media_review_unexpected_exception_internal(self, server: Any, tmp_path: Any) -> None:
        fake = _FakeMediaService()
        fake.review_media_error = RuntimeError("boom")
        _inject_media_service(server, fake)

        raw, ct = _make_multipart(b"DATA", "a.png", media_type="image")
        _, body = _decode(
            _call(server, path="/ext/review_service/reviews/media-review", method="POST", raw_body=raw, headers={"Content-Type": ct})
        )
        assert body["error"]["code"] == "INTERNAL"

    def test_media_review_missing_file_field_400(self, server: Any) -> None:
        raw = base64.b64encode(b"----b\r\nnot multipart\r\n----b--").decode()
        status, body = _decode(
            _call(
                server,
                path="/ext/review_service/reviews/media-review",
                method="POST",
                raw_body=raw,
                headers={"Content-Type": "multipart/form-data; boundary=b"},
            )
        )
        assert status == 400
        assert "file" in body["error"]["message"]

    def test_media_review_non_multipart_400(self, server: Any) -> None:
        raw = base64.b64encode(b"plain").decode()
        status, body = _decode(
            _call(
                server,
                path="/ext/review_service/reviews/media-review",
                method="POST",
                raw_body=raw,
                headers={"Content-Type": "application/json"},
            )
        )
        assert status == 400
        assert "multipart" in body["error"]["message"]

    def test_media_review_bad_base64_400(self, server: Any) -> None:
        status, body = _decode(
            _call(
                server,
                path="/ext/review_service/reviews/media-review",
                method="POST",
                raw_body="!!!not-base64!!!",
                headers={"Content-Type": "multipart/form-data; boundary=b"},
            )
        )
        assert status == 400
        assert "invalid upload body" in body["error"]["message"]


# ---------------------------------------------------------------------------
# GET /reviews/{review_id}/media-metadata
# ---------------------------------------------------------------------------


class TestMediaMetadataEndpoint:
    def test_metadata_missing_review(self, server: Any) -> None:
        _, body = _decode(_call(server, path="/ext/review_service/reviews/nope/media-metadata", method="GET"))
        assert body["error"]["code"] == "NOT_FOUND"

    def test_metadata_returns_stored_results(self, server: Any) -> None:
        rid = _create(server, metadata={"media_review_results": {"path.png": {"is_valid": True}}})
        _, body = _decode(_call(server, path=f"/ext/review_service/reviews/{rid}/media-metadata", method="GET"))
        assert body["review_id"] == rid
        assert body["media_metadata"] == {"path.png": {"is_valid": True}}

    def test_metadata_empty_when_no_media_files(self, server: Any) -> None:
        rid = _create(server)
        _, body = _decode(_call(server, path=f"/ext/review_service/reviews/{rid}/media-metadata", method="GET"))
        assert body["media_metadata"] == []

    def test_metadata_regenerates_from_files(self, server: Any, tmp_path: Any) -> None:
        p = tmp_path / "a.png"
        p.write_bytes(b"png")
        rid = _create(server, metadata={"media_files": [{"path": str(p), "media_type": "image"}]})

        fake = _FakeMediaService()
        fake.metadata_result = {"format": "PNG", "width": 8}
        _inject_media_service(server, fake)

        _, body = _decode(_call(server, path=f"/ext/review_service/reviews/{rid}/media-metadata", method="GET"))
        assert body["media_metadata"] == [
            {"file_path": str(p), "media_type": "image", "format": "PNG", "width": 8}
        ]

    def test_metadata_missing_file_entry(self, server: Any) -> None:
        rid = _create(server, metadata={"media_files": [{"path": "/no/such/file.png", "media_type": "image"}]})
        _, body = _decode(_call(server, path=f"/ext/review_service/reviews/{rid}/media-metadata", method="GET"))
        assert body["media_metadata"] == [{"file_path": "/no/such/file.png", "error": "文件不存在或路径无效"}]

    def test_metadata_regenerate_error_entry(self, server: Any, tmp_path: Any) -> None:
        p = tmp_path / "a.png"
        p.write_bytes(b"png")
        rid = _create(server, metadata={"media_files": [str(p)]})  # 无 media_type → 推断 image

        fake = _FakeMediaService()
        fake.metadata_error = FileNotFoundError("文件不存在")
        _inject_media_service(server, fake)

        # 推断成功但解析失败 → error 条目
        _, body = _decode(_call(server, path=f"/ext/review_service/reviews/{rid}/media-metadata", method="GET"))
        assert body["media_metadata"][0]["error"] == "文件不存在"

    def test_metadata_infer_failure_entry(self, server: Any, tmp_path: Any) -> None:
        p = tmp_path / "a.txt"
        p.write_bytes(b"text")
        rid = _create(server, metadata={"media_files": [str(p)]})  # .txt → 推断失败

        _, body = _decode(_call(server, path=f"/ext/review_service/reviews/{rid}/media-metadata", method="GET"))
        assert body["media_metadata"][0]["error"].startswith("无法推断媒体类型")


# ---------------------------------------------------------------------------
# POST /reviews/{review_id}/attachments
# ---------------------------------------------------------------------------


class TestAttachmentsEndpoint:
    def test_attachments_missing_review(self, server: Any) -> None:
        _, body = _decode(
            _call(
                server,
                path="/ext/review_service/reviews/nope/attachments",
                method="POST",
                raw_body=_body({"files": [{"path": "x.png", "media_type": "image"}]}),
            )
        )
        assert body["error"]["code"] == "NOT_FOUND"

    def test_attachments_empty_files_invalid(self, server: Any) -> None:
        rid = _create(server)
        _, body = _decode(
            _call(server, path=f"/ext/review_service/reviews/{rid}/attachments", method="POST", raw_body=_body({"files": []}))
        )
        assert body["error"]["code"] == "INVALID"

    def test_attachments_add_without_auto_review(self, server: Any) -> None:
        rid = _create(server)
        _, body = _decode(
            _call(
                server,
                path=f"/ext/review_service/reviews/{rid}/attachments",
                method="POST",
                raw_body=_body({"files": [{"path": "/tmp/a.png", "media_type": "image"}]}),
            )
        )
        assert body["added_count"] == 1
        assert body["attachments"][0]["path"] == "/tmp/a.png"
        assert body["attachments"][0]["review_result"] is None

        _, detail = _decode(_call(server, path=f"/ext/review_service/reviews/{rid}", method="GET"))
        assert detail["metadata"]["media_files"] == [{"path": "/tmp/a.png", "media_type": "image"}]

    def test_attachments_infer_media_type(self, server: Any) -> None:
        rid = _create(server)
        _, body = _decode(
            _call(
                server,
                path=f"/ext/review_service/reviews/{rid}/attachments",
                method="POST",
                raw_body=_body({"files": [{"path": "/tmp/clip.mp4"}]}),
            )
        )
        assert body["attachments"][0]["media_type"] == "video"

    def test_attachments_infer_failure_entry(self, server: Any) -> None:
        rid = _create(server)
        _, body = _decode(
            _call(
                server,
                path=f"/ext/review_service/reviews/{rid}/attachments",
                method="POST",
                raw_body=_body({"files": [{"path": "/tmp/doc.txt"}]}),
            )
        )
        assert body["attachments"][0]["error"] == "无法推断媒体类型"
        assert body["added_count"] == 1

    def test_attachments_missing_path_entry(self, server: Any) -> None:
        rid = _create(server)
        _, body = _decode(
            _call(
                server,
                path=f"/ext/review_service/reviews/{rid}/attachments",
                method="POST",
                raw_body=_body({"files": [{}]}),
            )
        )
        assert body["attachments"][0]["error"] == "缺少 path 字段"

    def test_attachments_auto_review_success(self, server: Any, tmp_path: Any) -> None:
        p = tmp_path / "a.png"
        p.write_bytes(b"png")

        fake = _FakeMediaService()
        fake.review_media_result = ImageReviewResult(is_valid=True, format="PNG", width=4)
        _inject_media_service(server, fake)

        rid = _create(server)
        _, body = _decode(
            _call(
                server,
                path=f"/ext/review_service/reviews/{rid}/attachments",
                method="POST",
                raw_body=_body({"files": [{"path": str(p), "media_type": "image"}], "auto_review": True}),
            )
        )
        assert body["attachments"][0]["review_result"]["is_valid"] is True

        _, detail = _decode(_call(server, path=f"/ext/review_service/reviews/{rid}", method="GET"))
        assert str(p) in detail["metadata"]["media_review_results"]

    def test_attachments_auto_review_failure_entry(self, server: Any, tmp_path: Any) -> None:
        p = tmp_path / "a.png"
        p.write_bytes(b"png")

        fake = _FakeMediaService()
        fake.review_media_error = RuntimeError("boom")
        _inject_media_service(server, fake)

        rid = _create(server)
        _, body = _decode(
            _call(
                server,
                path=f"/ext/review_service/reviews/{rid}/attachments",
                method="POST",
                raw_body=_body({"files": [{"path": str(p), "media_type": "image"}], "auto_review": True}),
            )
        )
        assert body["attachments"][0]["review_result"] == {"error": "boom"}

    def test_attachments_auto_review_missing_file_error(self, server: Any, tmp_path: Any) -> None:
        fake = _FakeMediaService()
        _inject_media_service(server, fake)

        rid = _create(server)
        _, body = _decode(
            _call(
                server,
                path=f"/ext/review_service/reviews/{rid}/attachments",
                method="POST",
                raw_body=_body({"files": [{"path": "/no/such.png", "media_type": "image"}], "auto_review": True}),
            )
        )
        assert body["attachments"][0]["review_result"] is None  # 文件不存在不审阅
        assert fake.called_with == []


# ---------------------------------------------------------------------------
# 分发层：未知路由 / 方法不匹配 / media-review 与 {review_id} 模板不串扰
# ---------------------------------------------------------------------------


class TestDispatchLayer:
    def test_unknown_path_404(self, server: Any) -> None:
        status, body = _decode(_call(server, path="/ext/review_service/reviews/abc/unknown", method="GET"))
        assert status == 404
        assert body["error"] == "not found"

    def test_wrong_method_404(self, server: Any) -> None:
        status, body = _decode(_call(server, path="/ext/review_service/reviews", method="DELETE"))
        assert status == 404

    def test_wrong_plugin_prefix_404(self, server: Any) -> None:
        status, body = _decode(_call(server, path="/ext/other_service/reviews", method="GET"))
        assert status == 404

    def test_media_review_not_shadowed_by_template(self, server: Any) -> None:
        """GET /reviews/media-review 命中 {review_id} 详情模板（对齐 channel_api 源语义）。

        源分发逻辑：POST /media-review 精确匹配；GET /media-review 落入
        '单级 path → get_review' 分支 → 审批不存在 NOT_FOUND（200+error body）。
        """
        status, body = _decode(_call(server, path="/ext/review_service/reviews/media-review", method="GET"))
        assert status == 200
        assert body["error"]["code"] == "NOT_FOUND"

    def test_media_review_template_not_shadowed(self, server: Any) -> None:
        """POST /reviews/{rid} 不存在（模板仅 GET），不应落到 media-review。"""
        status, body = _decode(
            _call(server, path="/ext/review_service/reviews/abc", method="POST", raw_body=_body({"title": "x"}))
        )
        assert status == 404

    def test_plugin_id_ignored(self, server: Any) -> None:
        status, body = _decode(
            _call(server, path="/ext/review_service/reviews", method="GET", plugin_id="whatever")
        )
        assert status == 200

    def test_missing_raw_body_defaults(self, server: Any) -> None:
        # GET 带空 raw_body/query 不炸
        status, body = _decode(_call(server, path="/ext/review_service/reviews", method="GET", raw_body=""))
        assert status == 200