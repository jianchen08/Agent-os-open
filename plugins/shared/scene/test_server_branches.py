# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""scene server.py 服务面分支测试：

- 未初始化守卫：各工具在 manager 就位前调用一律 success=False + "服务未初始化"
- MCP 工具面：create（含 layout 覆盖/无效模板 ValueError/未知异常）/switch/delete/
  list/get/update/get_active/list_templates 两态分支
- http.handle：path 前缀守卫 / 7 端点分发 / no-route 404 / SceneHTTPError 转
  对应状态 / 未预期异常转 500
- on_load / on_unload 生命周期

SceneManager 注入 tmp_path 持久化，零真实目录写入。
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

_PLUGIN_DIR = Path(__file__).resolve().parent
_SYSTEM_DIR = _PLUGIN_DIR.parent
_SCENE_PKG_PARENT = _SYSTEM_DIR  # scene 包以 system/ 为根导入


def _load_server() -> Any:
    mod_name = "scene_server_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    d = str(_PLUGIN_DIR)
    while d in sys.path:
        sys.path.remove(d)
    sys.path.insert(0, d)
    if str(_SCENE_PKG_PARENT) not in sys.path:
        sys.path.insert(0, str(_SCENE_PKG_PARENT))
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def srv(tmp_path: Path) -> Any:
    module = _load_server()
    from scene.manager import SceneManager
    from scene.persistence import ScenePersistence

    module._manager = SceneManager(persistence=ScenePersistence(tmp_path / "scenes"))
    # http.handle 的业务面单例同样指向 tmp 存储
    import routes_scene

    module._routes_scene = routes_scene
    monkey_holder = routes_scene._scene_manager
    routes_scene._scene_manager = module._manager
    yield module
    routes_scene._scene_manager = monkey_holder
    module._manager = None


@pytest.fixture
def bare_srv() -> Any:
    module = _load_server()
    yield module
    module._manager = None


async def test_all_tools_guard_uninitialized(bare_srv: Any) -> None:
    guard = {"success": False, "error": "服务未初始化"}
    assert await bare_srv.scene_create("n") == guard
    assert await bare_srv.scene_switch("s") == guard
    assert await bare_srv.scene_delete("s") == guard
    assert await bare_srv.scene_list() == guard
    assert await bare_srv.scene_get("s") == guard
    assert await bare_srv.scene_update("s") == guard
    assert await bare_srv.scene_get_active() == guard


async def test_create_and_get_roundtrip(srv: Any) -> None:
    created = await srv.scene_create("场景甲", description="描述", layout={"columns": 2})
    assert created["success"] is True
    scene_id = created["scene"]["id"]

    got = await srv.scene_get(scene_id)
    assert got["success"] is True
    assert got["scene"]["name"] == "场景甲"

    missing = await srv.scene_get("no-such")
    assert missing == {"success": False, "error": "场景不存在: no-such"}


async def test_create_invalid_template_valueerror(srv: Any) -> None:
    result = await srv.scene_create("坏模板", template_id="no-such-template")
    assert result["success"] is False
    assert isinstance(result["error"], str) and result["error"]


async def test_create_unexpected_exception_logged(srv: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(**_kw: Any) -> Any:
        raise RuntimeError("存储爆炸")

    monkeypatch.setattr(srv._manager, "create_scene", _boom)
    result = await srv.scene_create("炸")
    assert result["success"] is False
    assert "存储爆炸" in result["error"]


async def test_switch_delete_list_lifecycle(srv: Any) -> None:
    a = await srv.scene_create("甲")
    b = await srv.scene_create("乙")
    ids = {a["scene"]["id"], b["scene"]["id"]}

    switched = await srv.scene_switch(b["scene"]["id"])
    assert switched["success"] is True
    bad_switch = await srv.scene_switch("no-such")
    assert bad_switch["success"] is False

    active = await srv.scene_get_active()
    assert active["success"] is True
    assert active["scene"]["id"] == b["scene"]["id"]

    listing = await srv.scene_list()
    assert listing["success"] is True
    assert {s["id"] for s in listing["scenes"]} == ids
    assert listing["count"] == 2

    assert await srv.scene_delete(a["scene"]["id"]) == {"success": True}
    assert await srv.scene_delete(a["scene"]["id"]) == {"success": False}
    assert (await srv.scene_list())["count"] == 1


async def test_update_two_branches(srv: Any) -> None:
    created = await srv.scene_create("旧名")
    scene_id = created["scene"]["id"]
    updated = await srv.scene_update(scene_id, name="新名", description="新描述")
    assert updated["success"] is True
    assert updated["scene"]["name"] == "新名"

    missing = await srv.scene_update("no-such", name="x")
    assert missing == {"success": False, "error": "场景不存在: no-such"}


async def test_list_templates_real(srv: Any) -> None:
    result = await srv.scene_list_templates()
    assert result["success"] is True
    assert result["count"] == len(result["templates"])
    assert isinstance(result["templates"], list)


async def test_on_load_and_unload(bare_srv: Any) -> None:
    await bare_srv._on_load({})
    assert bare_srv._manager is not None
    await bare_srv._on_unload({})
    assert bare_srv._manager is None


# ── http.handle 分发面 ──


def _unwrapped(result: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """解 http.handle 信封：{success, data:{status, body(base64)}} → (status, dict)。"""
    data = result["data"]
    body = json.loads(base64.b64decode(data["body"]).decode("utf-8"))
    return int(data["status"]), body


async def test_http_rejects_non_scenes_path(srv: Any) -> None:
    result = await srv.http_handle(path="/ext/other_service/x", method="GET")
    status, body = _unwrapped(result)
    assert status == 404
    assert "not a scenes path" in body["error"]


async def test_http_list_and_create_endpoints(srv: Any) -> None:
    listed = await srv.http_handle(path="/ext/scene_service/scenes", method="GET")
    status, body = _unwrapped(listed)
    assert status == 200
    assert body["total"] == len(body["items"])

    created = await srv.http_handle(
        path="/ext/scene_service/scenes",
        method="POST",
        raw_body=json.dumps({"name": "HTTP 场景"}),
    )
    status, body = _unwrapped(created)
    assert status == 200
    assert body["name"] == "HTTP 场景"


async def test_http_templates_endpoint(srv: Any) -> None:
    result = await srv.http_handle(path="/ext/scene_service/scenes/templates", method="GET")
    status, body = _unwrapped(result)
    assert status == 200
    assert body["total"] == len(body["items"])


async def test_http_scene_id_crud_and_switch(srv: Any) -> None:
    created = await srv.http_handle(
        path="/ext/scene_service/scenes",
        method="POST",
        raw_body=json.dumps({"name": "HTTP 甲"}),
    )
    _, created_body = _unwrapped(created)
    scene_id = created_body["id"]

    got = await srv.http_handle(path=f"/ext/scene_service/scenes/{scene_id}", method="GET")
    _, got_body = _unwrapped(got)
    assert got_body["id"] == scene_id

    put = await srv.http_handle(
        path=f"/ext/scene_service/scenes/{scene_id}",
        method="PUT",
        raw_body=json.dumps({"name": "HTTP 改"}),
    )
    _, put_body = _unwrapped(put)
    assert put_body["name"] == "HTTP 改"

    switched = await srv.http_handle(path=f"/ext/scene_service/scenes/{scene_id}/switch", method="POST")
    status, _ = _unwrapped(switched)
    assert status == 200

    deleted = await srv.http_handle(path=f"/ext/scene_service/scenes/{scene_id}", method="DELETE")
    _, deleted_body = _unwrapped(deleted)
    assert deleted_body["success"] is True


async def test_http_scene_id_not_found_maps_status(srv: Any) -> None:
    result = await srv.http_handle(path="/ext/scene_service/scenes/ghost", method="GET")
    status, body = _unwrapped(result)
    assert status == 404
    assert "detail" in body


async def test_http_no_route_404(srv: Any) -> None:
    result = await srv.http_handle(path="/ext/scene_service/scenes/x/y/z", method="GET")
    status, body = _unwrapped(result)
    assert status == 404
    assert body["error"] == "not found"


async def test_http_unexpected_error_maps_500(srv: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import routes_scene

    def _boom(*_a: Any) -> Any:
        raise RuntimeError("路由层爆炸")

    monkeypatch.setattr(routes_scene, "list_scenes", _boom)
    result = await srv.http_handle(path="/ext/scene_service/scenes", method="GET")
    status, body = _unwrapped(result)
    assert status == 500
    assert "路由层爆炸" in body["detail"]
