# @feature: FP-0.2.二 scene 插件 http 面 | @vision: V3 可嵌入 | @ci: python-coverage
"""scene 插件 scenes 域 7 端点测试（channel_api 侧车化承接）。

覆盖（对齐原 routes_scene.py 语义 + 新 http.handle 分发层）：
1. POST/GET /scenes —— 创建/列表
2. GET /scenes/templates —— 模板列表
3. GET/PUT/DELETE /scenes/{scene_id} —— 详情/更新/删除
4. POST /scenes/{scene_id}/switch —— 切换活跃场景
5. 404 未知路由/未知场景、400 模板不存在（body {"detail": ...} 形态对齐 FastAPI 版）
6. plugin.json http_endpoints 声明 ↔ 分发路径对齐断言（7 端点、auth=user）

外部依赖：scene 命名空间包（插件目录自身）直接可用；SceneManager 以
tmp_path 持久化注入（不走仓库 data/），不接真实内核。
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

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "system" / "scene"


def _load_server() -> Any:
    """动态加载 scene/server.py（每次新建，隔离模块级状态）。"""
    spec = importlib.util.spec_from_file_location(
        "scene_server_http_test",
        str(_PLUGIN_DIR / "server.py"),
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scene_server_http_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server() -> Any:
    return _load_server()


@pytest.fixture
def rsc() -> Any:
    """routes_scene 模块（与 server 分发时 import 的同一模块对象）。"""
    import routes_scene  # noqa: PLC0415

    return routes_scene


@pytest.fixture
def scenes(rsc: Any, tmp_path: Path) -> None:
    """注入 tmp_path 持久化的 SceneManager 单例（每测试独立状态）。"""
    from scene.manager import SceneManager  # noqa: PLC0415
    from scene.persistence import ScenePersistence  # noqa: PLC0415

    rsc._scene_manager = SceneManager(  # type: ignore[attr-defined]
        persistence=ScenePersistence(storage_path=str(tmp_path / "scenes"))
    )


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


def _b64(payload: dict[str, Any]) -> str:
    """把 dict 编码为 base64 raw_body（http.handle body_encoding=base64 形态）。"""
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


# ── manifest ↔ 分发对齐 ───────────────────────────────────────────────


def test_manifest_declares_7_scenes_endpoints() -> None:
    """plugin.json http_endpoints 声明 7 端点，路径/方法/鉴权与分发层对齐。"""
    manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    eps = manifest["http_endpoints"]
    by_id = {e["route_id"]: e for e in eps}
    assert set(by_id) == {
        "scenes_create",
        "scenes_list",
        "scenes_templates",
        "scene_get",
        "scene_update",
        "scene_delete",
        "scene_switch",
    }
    expected_paths = {
        ("POST", "/ext/scene_service/scenes"),
        ("GET", "/ext/scene_service/scenes"),
        ("GET", "/ext/scene_service/scenes/templates"),
        ("GET", "/ext/scene_service/scenes/{scene_id}"),
        ("PUT", "/ext/scene_service/scenes/{scene_id}"),
        ("DELETE", "/ext/scene_service/scenes/{scene_id}"),
        ("POST", "/ext/scene_service/scenes/{scene_id}/switch"),
    }
    assert {(e["method"], e["path"]) for e in eps} == expected_paths
    for e in eps:
        assert e["auth"] == "user"
        assert e["handler_capability"] == "http.handle"
        assert e["timeout_ms"] == 5000


# ── scenes 域端点 ─────────────────────────────────────────────────────


def test_create_and_list_scenes(server: Any, scenes: None) -> None:
    status, body = _decode_http(
        _call(
            server,
            path="/ext/scene_service/scenes",
            method="POST",
            raw_body=_b64({"name": "研究场景", "description": "场景描述"}),
        )
    )
    assert status == 200
    scene = body
    assert scene["name"] == "研究场景"
    assert scene["description"] == "场景描述"
    assert scene["id"]

    status, body = _decode_http(_call(server, path="/ext/scene_service/scenes", method="GET"))
    assert status == 200
    assert body["total"] == 1
    assert body["items"][0]["id"] == scene["id"]


def test_create_scene_unknown_template_400(server: Any, scenes: None) -> None:
    status, body = _decode_http(
        _call(
            server,
            path="/ext/scene_service/scenes",
            method="POST",
            raw_body=_b64({"name": "x", "template_id": "no_such_template"}),
        )
    )
    assert status == 400
    assert "detail" in body


def test_list_templates(server: Any, scenes: None) -> None:
    status, body = _decode_http(_call(server, path="/ext/scene_service/scenes/templates", method="GET"))
    assert status == 200
    assert body["total"] >= 1
    for item in body["items"]:
        assert "id" in item
        assert "name" in item


def test_get_scene_by_id(server: Any, scenes: None) -> None:
    _, created = _decode_http(
        _call(
            server,
            path="/ext/scene_service/scenes",
            method="POST",
            raw_body=_b64({"name": "场景A"}),
        )
    )
    status, body = _decode_http(
        _call(server, path=f"/ext/scene_service/scenes/{created['id']}", method="GET")
    )
    assert status == 200
    assert body["id"] == created["id"]
    assert body["name"] == "场景A"


def test_get_missing_scene_404(server: Any, scenes: None) -> None:
    status, body = _decode_http(
        _call(server, path="/ext/scene_service/scenes/nope", method="GET")
    )
    assert status == 404
    assert body == {"detail": "场景 'nope' 不存在"}


def test_update_scene(server: Any, scenes: None) -> None:
    _, created = _decode_http(
        _call(
            server,
            path="/ext/scene_service/scenes",
            method="POST",
            raw_body=_b64({"name": "旧名"}),
        )
    )
    status, body = _decode_http(
        _call(
            server,
            path=f"/ext/scene_service/scenes/{created['id']}",
            method="PUT",
            raw_body=_b64({"name": "新名", "description": "新描述"}),
        )
    )
    assert status == 200
    assert body["name"] == "新名"
    assert body["description"] == "新描述"


def test_update_missing_scene_404(server: Any, scenes: None) -> None:
    status, body = _decode_http(
        _call(
            server,
            path="/ext/scene_service/scenes/nope",
            method="PUT",
            raw_body=_b64({"name": "x"}),
        )
    )
    assert status == 404
    assert "不存在" in body["detail"]


def test_delete_scene(server: Any, scenes: None) -> None:
    _, created = _decode_http(
        _call(
            server,
            path="/ext/scene_service/scenes",
            method="POST",
            raw_body=_b64({"name": "待删"}),
        )
    )
    status, body = _decode_http(
        _call(server, path=f"/ext/scene_service/scenes/{created['id']}", method="DELETE")
    )
    assert status == 200
    assert body == {"success": True, "message": "场景已删除"}
    _, after = _decode_http(_call(server, path="/ext/scene_service/scenes", method="GET"))
    assert after["total"] == 0


def test_delete_missing_scene_404(server: Any, scenes: None) -> None:
    status, body = _decode_http(
        _call(server, path="/ext/scene_service/scenes/nope", method="DELETE")
    )
    assert status == 404
    assert "不存在" in body["detail"]


def test_switch_scene(server: Any, scenes: None) -> None:
    _, a = _decode_http(
        _call(server, path="/ext/scene_service/scenes", method="POST", raw_body=_b64({"name": "A"}))
    )
    _, b = _decode_http(
        _call(server, path="/ext/scene_service/scenes", method="POST", raw_body=_b64({"name": "B"}))
    )
    status, body = _decode_http(
        _call(server, path=f"/ext/scene_service/scenes/{b['id']}/switch", method="POST")
    )
    assert status == 200
    assert body["id"] == b["id"]

    # 活跃场景持久化：重新读取列表可见
    _, listing = _decode_http(_call(server, path="/ext/scene_service/scenes", method="GET"))
    assert listing["total"] == 2
    # 切换回 A
    _, body_a = _decode_http(
        _call(server, path=f"/ext/scene_service/scenes/{a['id']}/switch", method="POST")
    )
    assert body_a["id"] == a["id"]


def test_switch_missing_scene_404(server: Any, scenes: None) -> None:
    status, body = _decode_http(
        _call(server, path="/ext/scene_service/scenes/nope/switch", method="POST")
    )
    assert status == 404


# ── 分发层边界 ────────────────────────────────────────────────────────


def test_unknown_subpath_404(server: Any, scenes: None) -> None:
    status, body = _decode_http(
        _call(server, path="/ext/scene_service/scenes/unknown/sub", method="GET")
    )
    assert status == 404
    assert body["error"] == "not found"


def test_wrong_method_404(server: Any, scenes: None) -> None:
    status, _ = _decode_http(_call(server, path="/ext/scene_service/scenes", method="DELETE"))
    assert status == 404


def test_non_scenes_path_404(server: Any, scenes: None) -> None:
    status, body = _decode_http(
        _call(server, path="/ext/scene_service/other", method="GET")
    )
    assert status == 404


def test_invalid_json_body_500(server: Any, scenes: None) -> None:
    status, _ = _decode_http(
        _call(server, path="/ext/scene_service/scenes", method="POST", raw_body="not-json{{{")
    )
    assert status == 500
