# @feature: FP-0.2.二 artifacts 插件 http 面 | @vision: V3 可嵌入 | @ci: python-coverage
"""artifacts 插件 artifacts + annotations 域 13 端点测试（channel_api 拆迁批次 1 侧车化承接）。

覆盖（对齐原 channel_api artifacts 分发语义 + 新 http.handle 分发层）：
1. POST /upload —— multipart 上传（成功/非 multipart/缺 file 字段/非法 base64）
2. GET/POST /artifacts —— 列表（task_id 过滤 + limit/offset + 非法 int 回退）/创建
3. GET/PUT/DELETE /artifacts/{id} —— 详情/更新（新版本）/删除 + NOT_FOUND
4. GET /artifacts/{id}/versions /diff —— 版本历史与差异
5. GET/POST /artifacts/{id}/annotations、PUT/DELETE /annotations/{id}、
   POST /annotations/{id}/resolve —— 批注全链路 + NOT_FOUND
6. 404 未知路由 / 错误 method / 非法 JSON 500 / 明文 JSON 解码

外部依赖（multimodal DiskFileStorage / tenant_data）经 env（UPLOADS_DIR /
MULTIMODAL_STORAGE_DIR → tmp）隔离，不落仓库 data/，不接真实内核。
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
    """动态加载 artifacts/server.py（每次新建，隔离模块级状态）。"""
    spec = importlib.util.spec_from_file_location(
        "artifacts_server_http_test",
        str(_PLUGIN_DIR / "server.py"),
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["artifacts_server_http_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server() -> Any:
    return _load_server()


@pytest.fixture
def storage_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """上传与元数据落 tmp（不污染仓库 data/）。"""
    uploads = tmp_path / "uploads"
    meta = tmp_path / "multimodal"
    monkeypatch.setenv("UPLOADS_DIR", str(uploads))
    monkeypatch.setenv("MULTIMODAL_STORAGE_DIR", str(meta))
    return str(uploads), str(meta)


def _multipart(
    filename: str = "a.png",
    content: bytes = b"\x89PNGfake",
    content_type: str = "image/png",
    thread_id: str = "th-1",
) -> tuple[str, str]:
    """构造 multipart/form-data 请求 → (raw_body base64, content-type)。"""
    parts = [
        f'--{_BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n".encode()
        + content
        + b"\r\n",
        f'--{_BOUNDARY}\r\nContent-Disposition: form-data; name="thread_id"\r\n\r\n{thread_id}\r\n'.encode(),
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
    """同步调用 http.handle（测试侧统一 asyncio 跑）。"""
    return _run(server.http_handle(**kwargs))


def _decode_http(result: dict[str, Any]) -> tuple[int, Any]:
    """解包 http.handle 返回 → (status, json_body)。"""
    assert result["success"], result
    resp = result["data"]
    body = base64.b64decode(resp["body"]).decode("utf-8")
    return resp["status"], json.loads(body)


def _make_artifact(server: Any, task_id: str = "t1", title: str = "报告") -> dict[str, Any]:
    """经 create 端点建一个制品，返回 artifact dict。"""
    _, body = _decode_http(
        _call(
            server,
            path="/ext/artifacts",
            method="POST",
            raw_body=json.dumps(
                {
                    "task_id": task_id,
                    "title": title,
                    "artifact_type": "text",
                    "content": "v1 content",
                    "metadata": {"k": "v"},
                }
            ),
        )
    )
    return body


# ═══════════════════════════════════════════════════════════
# 1. 上传（multipart）
# ═══════════════════════════════════════════════════════════


class TestUpload:
    def test_upload_success(self, server: Any, storage_dirs: tuple[str, str]) -> None:
        uploads, meta = storage_dirs
        raw, content_type = _multipart()
        status, body = _decode_http(
            _call(
                server,
                path="/ext/artifacts/upload",
                method="POST",
                raw_body=raw,
                headers={"content-type": content_type},
            )
        )
        assert status == 200
        assert body["filename"] == "a.png"
        assert body["mime_type"] == "image/png"
        assert body["media_type"] == "image"
        assert body["size"] == len(b"\x89PNGfake")
        assert body["url"] == f"/uploads/{body['file_id']}.png"
        assert (Path(uploads) / f"{body['file_id']}.png").exists()
        assert (Path(meta) / f"{body['file_id']}.json").exists()

    def test_upload_plain_json_content_type(self, server: Any) -> None:
        status, body = _decode_http(
            _call(
                server,
                path="/ext/artifacts/upload",
                method="POST",
                raw_body=base64.b64encode(b"whatever").decode(),
                headers={"content-type": "application/json"},
            )
        )
        assert status == 400
        assert "multipart/form-data" in body["error"]

    def test_upload_missing_file_field(self, server: Any) -> None:
        parts = [
            f'--{_BOUNDARY}\r\nContent-Disposition: form-data; name="thread_id"\r\n\r\nth-1\r\n'.encode(),
            f"--{_BOUNDARY}--\r\n".encode(),
        ]
        raw = base64.b64encode(b"".join(parts)).decode()
        status, body = _decode_http(
            _call(
                server,
                path="/ext/artifacts/upload",
                method="POST",
                raw_body=raw,
                headers={"content-type": f"multipart/form-data; boundary={_BOUNDARY}"},
            )
        )
        assert status == 400
        assert "missing 'file' field" in body["error"]

    def test_upload_invalid_base64(self, server: Any) -> None:
        status, body = _decode_http(
            _call(
                server,
                path="/ext/artifacts/upload",
                method="POST",
                raw_body="!!!!not-base64!!!!",
                headers={"content-type": "multipart/form-data; boundary=x"},
            )
        )
        assert status == 400
        assert "invalid upload body" in body["error"]

    def test_upload_document_media_type(self, server: Any, storage_dirs: tuple[str, str]) -> None:
        raw, content_type = _multipart(filename="doc.pdf", content_type="application/pdf")
        status, body = _decode_http(
            _call(
                server,
                path="/ext/artifacts/upload",
                method="POST",
                raw_body=raw,
                headers={"content-type": content_type},
            )
        )
        assert status == 200
        assert body["media_type"] == "document"
        assert body["url"].endswith(".pdf")


# ═══════════════════════════════════════════════════════════
# 2. 制品集合（list / create）
# ═══════════════════════════════════════════════════════════


class TestArtifactCollection:
    def test_list_empty_without_task_id(self, server: Any) -> None:
        status, body = _decode_http(_call(server, path="/ext/artifacts", method="GET"))
        assert status == 200
        assert body == {"items": [], "total": 0}

    def test_create_and_list(self, server: Any) -> None:
        art = _make_artifact(server, task_id="t1")
        status, body = _decode_http(
            _call(
                server,
                path="/ext/artifacts",
                method="GET",
                query={"task_id": "t1"},
            )
        )
        assert status == 200
        assert body["total"] == 1
        assert body["items"][0]["id"] == art["id"]
        assert body["items"][0]["title"] == "报告"
        assert body["items"][0]["metadata"] == {"k": "v"}

    def test_list_limit_offset(self, server: Any) -> None:
        for i in range(3):
            _make_artifact(server, task_id="t2", title=f"制品{i}")
        status, body = _decode_http(
            _call(
                server,
                path="/ext/artifacts",
                method="GET",
                query={"task_id": "t2", "limit": "2", "offset": "1"},
            )
        )
        assert status == 200
        assert body["total"] == 3
        assert len(body["items"]) == 2

    def test_list_bad_int_falls_back(self, server: Any) -> None:
        _make_artifact(server, task_id="t3")
        status, body = _decode_http(
            _call(
                server,
                path="/ext/artifacts",
                method="GET",
                query={"task_id": "t3", "limit": "abc", "offset": "-x"},
            )
        )
        assert status == 200
        assert body["total"] == 1


# ═══════════════════════════════════════════════════════════
# 3. 制品单条（get / update / delete）
# ═══════════════════════════════════════════════════════════


class TestArtifactItem:
    def test_get_artifact(self, server: Any) -> None:
        art = _make_artifact(server)
        status, body = _decode_http(
            _call(
                server,
                path=f"/ext/artifacts/{art['id']}",
                method="GET",
            )
        )
        assert status == 200
        assert body["id"] == art["id"]

    def test_get_artifact_missing(self, server: Any) -> None:
        status, body = _decode_http(
            _call(
                server,
                path="/ext/artifacts/ghost-123",
                method="GET",
            )
        )
        assert status == 200
        assert body["error"]["code"] == "NOT_FOUND"

    def test_update_artifact_creates_version(self, server: Any) -> None:
        art = _make_artifact(server)
        status, body = _decode_http(
            _call(
                server,
                path=f"/ext/artifacts/{art['id']}",
                method="PUT",
                raw_body='{"content": "v2 content", "title": "报告v2"}',
            )
        )
        assert status == 200
        assert body["version"] == 2
        assert body["content"] == "v2 content"
        assert body["parent_artifact_id"] == art["id"]

    def test_update_artifact_missing(self, server: Any) -> None:
        status, body = _decode_http(
            _call(
                server,
                path="/ext/artifacts/ghost-123",
                method="PUT",
                raw_body='{"content": "x"}',
            )
        )
        assert status == 200
        assert body["error"]["code"] == "NOT_FOUND"

    def test_delete_artifact(self, server: Any) -> None:
        art = _make_artifact(server)
        status, body = _decode_http(
            _call(
                server,
                path=f"/ext/artifacts/{art['id']}",
                method="DELETE",
            )
        )
        assert status == 200
        assert body == {"success": True}
        _, gone = _decode_http(
            _call(
                server,
                path=f"/ext/artifacts/{art['id']}",
                method="GET",
            )
        )
        assert gone["error"]["code"] == "NOT_FOUND"

    def test_delete_artifact_missing(self, server: Any) -> None:
        status, body = _decode_http(
            _call(
                server,
                path="/ext/artifacts/ghost-123",
                method="DELETE",
            )
        )
        assert status == 200
        assert body == {"success": False}


# ═══════════════════════════════════════════════════════════
# 4. 版本历史 / 差异
# ═══════════════════════════════════════════════════════════


class TestVersions:
    def test_version_history(self, server: Any) -> None:
        art = _make_artifact(server, title="V")
        _, v2 = _decode_http(
            _call(
                server,
                path=f"/ext/artifacts/{art['id']}",
                method="PUT",
                raw_body='{"content": "v2"}',
            )
        )
        # 版本链从最新制品 id 向上追溯（v1 起点的链只有自身）
        status, body = _decode_http(
            _call(
                server,
                path=f"/ext/artifacts/{v2['id']}/versions",
                method="GET",
            )
        )
        assert status == 200
        assert body["total"] == 2
        assert body["items"][0]["version"] == 2
        assert body["items"][1]["version"] == 1

    def test_version_history_missing(self, server: Any) -> None:
        status, body = _decode_http(
            _call(
                server,
                path="/ext/artifacts/ghost/versions",
                method="GET",
            )
        )
        assert status == 200
        assert body == {"items": [], "total": 0}

    def test_version_diff(self, server: Any) -> None:
        art = _make_artifact(server, title="D")
        _, v2 = _decode_http(
            _call(
                server,
                path=f"/ext/artifacts/{art['id']}",
                method="PUT",
                raw_body='{"content": "v2 differs"}',
            )
        )
        status, body = _decode_http(
            _call(
                server,
                path=f"/ext/artifacts/{v2['id']}/diff",
                method="GET",
                query={"from": "1", "to": "2"},
            )
        )
        assert status == 200
        assert body["from_version"] == 1
        assert body["to_version"] == 2
        assert "-v1 content" in body["diff"]
        assert "+v2 differs" in body["diff"]

    def test_version_diff_default_versions(self, server: Any) -> None:
        art = _make_artifact(server, title="D2")
        _, v2 = _decode_http(
            _call(
                server,
                path=f"/ext/artifacts/{art['id']}",
                method="PUT",
                raw_body='{"content": "v2"}',
            )
        )
        status, body = _decode_http(
            _call(
                server,
                path=f"/ext/artifacts/{v2['id']}/diff",
                method="GET",
            )
        )
        assert status == 200
        assert body["from_version"] == 1
        assert body["to_version"] == 2
        assert body["diff"]


# ═══════════════════════════════════════════════════════════
# 5. 批注全链路
# ═══════════════════════════════════════════════════════════


class TestAnnotations:
    def test_create_and_list_annotations(self, server: Any) -> None:
        art = _make_artifact(server)
        status, body = _decode_http(
            _call(
                server,
                path=f"/ext/artifacts/{art['id']}/annotations",
                method="POST",
                raw_body=json.dumps(
                    {
                        "target_type": "text_selection",
                        "target_data": {"line": 3},
                        "content": "这里有问题",
                        "author_type": "user",
                        "author_id": "u-1",
                    }
                ),
            )
        )
        assert status == 200
        assert body["content"] == "这里有问题"
        assert body["target_type"] == "text_selection"

        status, listed = _decode_http(
            _call(
                server,
                path=f"/ext/artifacts/{art['id']}/annotations",
                method="GET",
            )
        )
        assert status == 200
        assert listed["total"] == 1
        assert listed["items"][0]["id"] == body["id"]

    def test_list_annotations_status_filter(self, server: Any) -> None:
        art = _make_artifact(server)
        _decode_http(
            _call(
                server,
                path=f"/ext/artifacts/{art['id']}/annotations",
                method="POST",
                raw_body=json.dumps({"content": "a1", "author_id": "u-1"}),
            )
        )
        status, body = _decode_http(
            _call(
                server,
                path=f"/ext/artifacts/{art['id']}/annotations",
                method="GET",
                query={"status": "resolved"},
            )
        )
        assert status == 200
        assert body["total"] == 0

    def test_update_annotation(self, server: Any) -> None:
        art = _make_artifact(server)
        _, ann = _decode_http(
            _call(
                server,
                path=f"/ext/artifacts/{art['id']}/annotations",
                method="POST",
                raw_body=json.dumps({"content": "old"}),
            )
        )
        status, body = _decode_http(
            _call(
                server,
                path=f"/ext/artifacts/annotations/{ann['id']}",
                method="PUT",
                raw_body=json.dumps({"content": "new", "target_data": {"x": 1}}),
            )
        )
        assert status == 200
        assert body["content"] == "new"
        assert body["target_data"] == {"x": 1}

    def test_update_annotation_missing(self, server: Any) -> None:
        status, body = _decode_http(
            _call(
                server,
                path="/ext/artifacts/annotations/ghost-ann",
                method="PUT",
                raw_body='{"content": "x"}',
            )
        )
        assert status == 200
        assert body["error"]["code"] == "NOT_FOUND"

    def test_delete_annotation(self, server: Any) -> None:
        art = _make_artifact(server)
        _, ann = _decode_http(
            _call(
                server,
                path=f"/ext/artifacts/{art['id']}/annotations",
                method="POST",
                raw_body=json.dumps({"content": "a"}),
            )
        )
        status, body = _decode_http(
            _call(
                server,
                path=f"/ext/artifacts/annotations/{ann['id']}",
                method="DELETE",
            )
        )
        assert status == 200
        assert body == {"success": True}

    def test_delete_annotation_missing(self, server: Any) -> None:
        status, body = _decode_http(
            _call(
                server,
                path="/ext/artifacts/annotations/ghost-ann",
                method="DELETE",
            )
        )
        assert status == 200
        assert body == {"success": False}

    def test_resolve_annotation(self, server: Any) -> None:
        art = _make_artifact(server)
        _, ann = _decode_http(
            _call(
                server,
                path=f"/ext/artifacts/{art['id']}/annotations",
                method="POST",
                raw_body=json.dumps({"content": "r"}),
            )
        )
        status, body = _decode_http(
            _call(
                server,
                path=f"/ext/artifacts/annotations/{ann['id']}/resolve",
                method="POST",
            )
        )
        assert status == 200
        assert body["status"] == "resolved"
        assert body["resolved_at"]

    def test_resolve_annotation_missing(self, server: Any) -> None:
        status, body = _decode_http(
            _call(
                server,
                path="/ext/artifacts/annotations/ghost-ann/resolve",
                method="POST",
            )
        )
        assert status == 200
        assert body["error"]["code"] == "NOT_FOUND"


# ═══════════════════════════════════════════════════════════
# 6. 分发层：404 / method / 非法 JSON
# ═══════════════════════════════════════════════════════════


class TestDispatch:
    def test_unknown_path_404(self, server: Any) -> None:
        # 三级未知路径不匹配任何模板 → 404（单级/两级段按 artifact_id 语义走 NOT_FOUND）
        status, body = _decode_http(
            _call(
                server,
                path="/ext/artifacts/a/b/c",
                method="GET",
            )
        )
        assert status == 404
        assert "not found" in body["error"]

    def test_wrong_method_404(self, server: Any) -> None:
        art = _make_artifact(server)
        status, body = _decode_http(
            _call(
                server,
                path=f"/ext/artifacts/{art['id']}/versions",
                method="POST",
            )
        )
        assert status == 404

    def test_invalid_json_body_500(self, server: Any) -> None:
        result = _call(server, path="/ext/artifacts", method="POST", raw_body="{not-json")
        assert result["success"] is False
        assert result["data"]["status"] == 500

    def test_plain_json_body_decoded(self, server: Any) -> None:
        status, body = _decode_http(
            _call(
                server,
                path="/ext/artifacts",
                method="POST",
                raw_body='{"task_id": "plain", "title": "t", "content": "c"}',
            )
        )
        assert status == 200
        assert body["task_id"] == "plain"

    def test_empty_body_create(self, server: Any) -> None:
        status, body = _decode_http(_call(server, path="/ext/artifacts", method="POST"))
        assert status == 200
        assert body["task_id"] == ""

    def test_services_and_tools_registered(self, server: Any) -> None:
        assert "http.handle" in server.plugin._tools
        assert server.get_artifact_service() is not None
        assert server.get_annotation_service() is not None
        assert server._infer_media_type("") == "document"
        assert server._infer_media_type("audio/mp3") == "audio"

    def test_on_load_smoke(self, server: Any) -> None:
        _run(server._on_load({}))  # 不抛即过
        assert server.plugin._lifecycle_handlers  # on_load 已注册


# ═══════════════════════════════════════════════════════════
# 7. 补充覆盖：引导分支 / base64 body / multipart 防御分支 / 上传目录默认值
# ═══════════════════════════════════════════════════════════


class TestExtraBranches:
    def test_load_adds_system_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """system / shared 目录不在 sys.path 时，server.py 自举补入（含 tenant_data）。"""
        sys_dir = str(_PLUGIN_DIR.parent)
        shared_dir = str(_PLUGIN_DIR.parents[1])
        prev = [p for p in list(sys.path) if p in (sys_dir, shared_dir)]
        for p in prev:
            sys.path.remove(p)
        try:
            loaded = _load_server()
            assert "http.handle" in loaded.plugin._tools
            assert sys_dir in sys.path
            assert shared_dir in sys.path
        finally:
            for p in prev:
                if p not in sys.path:
                    sys.path.insert(0, p)

    def test_put_artifact_with_base64_body(self, server: Any) -> None:
        """raw_body 为 base64 编码 JSON → _decode_body 走 base64 分支。"""
        art = _make_artifact(server)
        status, body = _decode_http(
            _call(
                server,
                path=f"/ext/artifacts/{art['id']}",
                method="PUT",
                raw_body=base64.b64encode(b'{"content": "v2 from base64"}').decode(),
            )
        )
        assert status == 200
        assert body["content"] == "v2 from base64"
        assert body["version"] == 2

    def test_parse_multipart_non_multipart(self, server: Any) -> None:
        """非 multipart content-type → 空字段字典。"""
        assert server._parse_multipart("text/plain", b"hello") == {}

    def test_parse_multipart_part_without_name(self, server: Any) -> None:
        """part 缺 name 参数 → 跳过该 part。"""
        body = f"--{_BOUNDARY}\r\nContent-Disposition: form-data\r\n\r\nno-name-value\r\n--{_BOUNDARY}--\r\n".encode()
        fields = server._parse_multipart(f"multipart/form-data; boundary={_BOUNDARY}", body)
        assert fields == {}

    def test_get_uploads_dir_default_tenant(self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """UPLOADS_DIR 未设 → 多租户数据根 data/{tenant}/uploads（AGENTOS_DATA_DIR 隔离）。"""
        monkeypatch.delenv("UPLOADS_DIR", raising=False)
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        assert server._get_uploads_dir() == str(tmp_path / "default" / "uploads")

    def test_upload_parse_failure(self, server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """_parse_multipart 抛错 → 400 multipart parse failed。"""

        def boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("malformed")

        monkeypatch.setattr(server, "_parse_multipart", boom)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/artifacts/upload",
                method="POST",
                raw_body=base64.b64encode(b"garbage").decode(),
                headers={"content-type": f"multipart/form-data; boundary={_BOUNDARY}"},
            )
        )
        assert status == 400
        assert "multipart parse failed" in body["error"]
