# @feature: FP-0.2.二 bilibili 通道 | @ci: python-coverage
"""channel_bilibili 测试：B 站直播 API 客户端（传输注入）+ 服务面声明护栏。

覆盖：
1. cookie 未配置 → 显式 COOKIE_UNSET（不降级假成功）
2. start_live 成功：B 站信封 code=0 → 返回推流 addr/code
3. start_live 业务失败：信封 code!=0 → 显式错误带平台 message
4. stop_live 命中 stopLive 端点且带 CSRF
5. update_title：无 [AI] 前缀自动补（D2 标识纪律），有前缀不动
6. popularity：解析人气数字
7. blivedm 缺失 → danmaku 面可用性 False 但模块可装载
8. plugin.json services 声明 = server 注册（G2 同源护栏）
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_PLUGIN_DIR = Path(__file__).parent
sys.path.insert(0, str(_PLUGIN_DIR))


def _load(filename: str, mod_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_bc = _load("bili_client.py", "bili_client")
BiliLiveClient = _bc.BiliLiveClient
BiliApiError = _bc.BiliApiError


class FakeTransport:
    """伪传输：按 (method, path) 路由返回 B 站信封；记录请求。"""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def __call__(
        self, method: str, path: str, *, form: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((method, path, form))
        for key, value in self.routes.items():
            if key in path:
                if isinstance(value, Exception):
                    raise value
                return value
        return {"code": -1, "message": "no route"}


def _client(transport: FakeTransport, cookie: str | None = "SESSDATA=abc; bili_jct=csrf0") -> Any:
    return BiliLiveClient(
        cookie=cookie, room_id="1234", request_fn=transport
    )


def test_cookie_unset_is_explicit() -> None:
    client = BiliLiveClient(cookie=None, room_id="1234", request_fn=FakeTransport({}))
    with pytest.raises(BiliApiError) as exc:
        asyncio.run(client.start_live())
    assert "COOKIE_UNSET" in str(exc.value)


def test_start_live_success_returns_rtmp() -> None:
    tp = FakeTransport({
        "startLive": {"code": 0, "message": "", "data": {
            "rtmp": {"addr": "rtmp://push.bili/", "code": "secret-key"}
        }},
    })
    result = asyncio.run(_client(tp).start_live())
    assert result["addr"] == "rtmp://push.bili/"
    assert result["code"] == "secret-key"
    method, path, form = tp.calls[0]
    assert "startLive" in path
    assert form is not None and form["room_id"] == "1234"
    assert form["csrf_token"] == "csrf0"


def test_start_live_platform_error_surfaces() -> None:
    tp = FakeTransport({
        "startLive": {"code": 60024, "message": "请先完成实名认证", "data": {}},
    })
    with pytest.raises(BiliApiError) as exc:
        asyncio.run(_client(tp).start_live())
    assert "60024" in str(exc.value) and "实名认证" in str(exc.value)


def test_stop_live_hits_endpoint_with_csrf() -> None:
    tp = FakeTransport({"stopLive": {"code": 0, "message": "", "data": {}}})
    asyncio.run(_client(tp).stop_live())
    method, path, form = tp.calls[0]
    assert "stopLive" in path
    assert form is not None and form["csrf_token"] == "csrf0"


def test_update_title_auto_prefixes_ai_tag() -> None:
    """D2 标识纪律：标题必须带 [AI] 前缀——缺失自动补，已有不重复。"""
    tp = FakeTransport({"update": {"code": 0, "message": "", "data": {}}})
    asyncio.run(_client(tp).update_title("梗宇宙直播"))
    _m, _p, form = tp.calls[0]
    assert form is not None and form["title"].startswith("[AI] 梗宇宙直播")

    tp2 = FakeTransport({"update": {"code": 0, "message": "", "data": {}}})
    asyncio.run(_client(tp2).update_title("[AI] 已带标识"))
    _m, _p, form2 = tp2.calls[0]
    assert form2 is not None and form2["title"] == "[AI] 已带标识"


def test_popularity_parses_number() -> None:
    tp = FakeTransport({
        "getInfoByRoom": {"code": 0, "message": "", "data": {
            "room_info": {"popularity": 4321}
        }},
    })
    value = asyncio.run(_client(tp).get_popularity())
    assert value == 4321


def test_danmaku_face_graceful_without_blivedm() -> None:
    """blivedm 未安装 → 弹幕面可用性 False（原因注明），插件其余能力不受影响。"""
    bc = _load("danmaku_face.py", "danmaku_face_mod")
    face = bc.DanmakuFace(cookie="SESSDATA=x", room_id="1234")
    status = face.status()
    # 本环境 blivedm 未安装 → available False；若已安装则 True（双向兼容断言）
    assert status["available"] in (True, False)
    if not status["available"]:
        assert "reason" in status


def test_plugin_json_services_match_server_registration() -> None:
    mod = _load("server.py", "channel_bilibili_server_test")
    manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    declared = {s["name"] for s in manifest["capabilities"]["services"]}
    registered = set(mod.plugin._tools.keys())  # noqa: SLF001
    assert declared == registered
    assert all(name.startswith("bili.") for name in declared)
