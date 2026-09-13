# @feature: FP-0.2.二 管道插件 | @ci: python-coverage
"""godot_context HTTP 面缺口补测（业务逻辑归 plugin.py 自身测试）：

- on_load：frontend capability 缺席 → emitter 不注入（告警分支）；注入 → set_emitter
- on_unload：单例复位
- execute：结果 dict 直传 / 结果对象转 state_updates（含 skip_remaining）
- http.handle 补充分支：subscribe 非法 body 容错、preview 索引解析与
  代理失败 502、未知路由 404、_fetch_preview 非 200/非 PNG/成功三态
"""

from __future__ import annotations

import base64
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "pipeline" / "input" / "godot_context"


def _load_server() -> Any:
    spec = importlib.util.spec_from_file_location(
        "godot_context_gaps_test_server",
        str(_PLUGIN_DIR / "server.py"),
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["godot_context_gaps_test_server"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server() -> Any:
    return _load_server()


class FakeInst:
    """GodotContextPlugin 替身：记录调用并回放脚本化返回。"""

    execute_result: object = {"state_updates": {}}

    def __init__(self) -> None:
        self.pushed: list[dict] = []
        self.subscribed: list[str] = []
        self.dismissed = 0

    async def handle_push(self, payload: dict) -> dict:
        self.pushed.append(payload)
        return {"success": True, "data": {"ok": True}}

    async def execute(self, ctx: object) -> object:
        return self.execute_result

    def snapshot(self) -> dict:
        return {"success": True, "data": {"selection": None}}

    async def dismiss(self) -> dict:
        self.dismissed += 1
        return {"success": True}

    def subscribe(self, thread_id: str) -> dict:
        self.subscribed.append(thread_id)
        return {"success": True, "data": {"thread": thread_id}}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, server: Any) -> FakeInst:
    inst = FakeInst()
    monkeypatch.setattr(server, "get_instance", lambda: inst)
    return inst


def _b64(obj: dict) -> str:
    import json

    return base64.b64encode(json.dumps(obj).encode()).decode()


async def test_on_load_without_frontend_capability_and_unload(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 缺席分支：from_plugin 返回 None → 不注入 emitter（告警）
    called: dict[str, bool] = {}

    class _DummyMod(types.SimpleNamespace):
        pass

    real_set_emitter = server.set_emitter

    def _spy(emitter: Any) -> None:
        called["set"] = True
        real_set_emitter(emitter)

    monkeypatch.setattr(server, "set_emitter", _spy)
    await server._on_load({})
    assert "set" not in called, "frontend capability 未注入时不得注入 emitter"
    assert server.get_instance() is not None

    # 注入分支：from_plugin 返回真 emitter → set_emitter 被调
    class FakeEmitter:
        @classmethod
        def from_plugin(cls, plugin: Any) -> object:
            return object()

    monkeypatch.setattr(server, "FrontendEmitter", FakeEmitter)
    await server._on_load({})
    assert called.get("set") is True

    await server._on_unload({})
    assert server._instance is None


async def test_execute_dict_passthrough_and_object_conversion(
    server: Any, fake: FakeInst, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 结果为 dict → 原样直传
    server.get_instance().execute_result = {"state_updates": {"k": "v"}}
    out = await server.execute(state={"messages": []})
    assert out == {"state_updates": {"k": "v"}}

    # 结果为对象 → 转 state_updates（含 skip_remaining 标记）
    class ObjResult:
        state_updates = {"a": 1}
        skip_remaining = True

    server.get_instance().execute_result = ObjResult()
    out2 = await server.execute(state={}, config={"x": 1})
    assert out2 == {"state_updates": {"a": 1}, "skip_remaining": True}


async def test_subscribe_bad_body_falls_back_to_empty(
    server: Any, fake: FakeInst
) -> None:
    bad = await server.http_handle(
        path="/ext/pipeline_godot_context/subscribe",
        method="POST",
        raw_body=base64.b64encode(b"not-json").decode(),
    )
    assert bad["success"] is True
    assert fake.subscribed == [""]


async def test_preview_routes(server: Any, fake: FakeInst, monkeypatch: pytest.MonkeyPatch) -> None:
    base = "/ext/pipeline_godot_context/preview"

    # 代理失败 → 502
    async def _none(index: int) -> None:
        return None

    monkeypatch.setattr(server, "_fetch_preview", _none)
    miss = await server.http_handle(path=base, method="GET", query={"index": "3"})
    assert miss["data"]["status"] == 502
    import json

    assert "preview unavailable" in json.loads(base64.b64decode(miss["data"]["body"]))["error"]

    # 索引非法 → 回落 0；PNG 成功 → base64 信封
    seen: dict[str, int] = {}

    async def _png(index: int) -> bytes | None:
        seen["index"] = index
        return b"\x89PNG\r\n\x1a\n" + b"rest"

    monkeypatch.setattr(server, "_fetch_preview", _png)
    ok = await server.http_handle(path=base, method="GET", query={"index": "abc"})
    assert seen["index"] == 0, "非法 index 回落 0"
    data = ok["data"]
    assert data["status"] == 200
    assert data["headers"]["Content-Type"] == "image/png"
    import base64 as _b64mod

    assert _b64mod.b64decode(data["body"]).startswith(b"\x89PNG")

    # 无 query → 默认 index 0
    await server.http_handle(path=base, method="GET")
    assert seen["index"] == 0


async def test_unknown_route_404(server: Any, fake: FakeInst) -> None:
    miss = await server.http_handle(path="/ext/pipeline_godot_context/nope", method="GET")
    assert miss["data"]["status"] == 404
    import json

    body = json.loads(base64.b64decode(miss["data"]["body"]))
    assert "nope" in body["error"]


async def test_fetch_preview_real_function_three_states(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_fetch_preview 本体：非 200 → None；非 PNG → None；成功 → PNG 字节。"""
    class FakeResp:
        def __init__(self, status: int, body: bytes) -> None:
            self.status = status
            self._body = body

        async def read(self) -> bytes:
            return self._body

        async def __aenter__(self) -> "FakeResp":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    class FakeSession:
        def __init__(self, resp: FakeResp, boom: bool = False) -> None:
            self._resp = resp
            self._boom = boom

        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        def get(self, url: str, params: dict | None = None) -> FakeResp:
            if self._boom:
                raise RuntimeError("net down")
            return self._resp

    def install(resp: FakeResp, boom: bool = False) -> None:
        fake_aiohttp = types.SimpleNamespace(
            ClientTimeout=lambda **kw: object(),
            ClientSession=lambda timeout=None: FakeSession(resp, boom),
        )
        monkeypatch.setitem(sys.modules, "aiohttp", fake_aiohttp)

    png = b"\x89PNG\r\n\x1a\n" + b"img"

    install(FakeResp(200, png))
    assert await server._fetch_preview(0) == png

    install(FakeResp(500, png))
    assert await server._fetch_preview(0) is None

    install(FakeResp(200, b"not-png"))
    assert await server._fetch_preview(0) is None

    install(FakeResp(200, png), boom=True)
    assert await server._fetch_preview(0) is None
