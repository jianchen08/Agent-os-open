# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""渠道 server.py 适配层测试（A5.2 补）。

server.py 是纯接口适配层：@plugin.on_load 建 adapter、@plugin.tool 暴露
send_message/handle_callback/get_status。用 importlib 独立加载 + 假 adapter
注入覆盖全部分支，不触发真实网络 I/O。

三个渠道（wecom/qq/dingtalk）共用同一 fixture 模式。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SYSTEM_DIR = Path(__file__).resolve().parents[2] / "plugins" / "shared" / "system"
_CC_DIR = _SYSTEM_DIR / "channel_common"

# 各通道目录下与 server.py 顶层 import 冲突的同名平铺模块
_EVICT = {
    "server", "adapter", "helpers", "stream_client", "crypto",
    "onebot_client", "input_adapter", "output_adapter", "base_combo_adapter",
    "pipeline_types", "card_builder", "message_normalizer",
}


@pytest.fixture()
def load_server(request: pytest.FixtureRequest):
    """按渠道加载 server 模块,teardown 清理 sys.path 与 sys.modules。"""
    channel: str = request.param
    d = str(_SYSTEM_DIR / f"channel_{channel}")
    cc = str(_CC_DIR)
    saved_path = list(sys.path)
    saved_modules = {m: sys.modules[m] for m in _EVICT if m in sys.modules}
    try:
        sys.path.insert(0, d)
        # 移除可能已被其他渠道测试加入的 channel_common,让 server.py 自身
        # 执行 append 接线分支(组合跑时该行确定性覆盖)
        while cc in sys.path:
            sys.path.remove(cc)
        for m in _EVICT:
            sys.modules.pop(m, None)
        mod_name = f"channel_{channel}_server_test"
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        spec = importlib.util.spec_from_file_location(mod_name, Path(d) / "server.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path[:] = saved_path
        sys.modules.pop(mod_name, None)
        # 恢复被逐出的模块（重新解析到正确的渠道目录）
        for name, mod in saved_modules.items():
            sys.modules[name] = mod


def _fake_stream_client() -> SimpleNamespace:
    return SimpleNamespace(_session=SimpleNamespace(), send_message=None)


def _fake_adapter(stream_client: Any = None) -> SimpleNamespace:
    sc = stream_client if stream_client is not None else _fake_stream_client()

    async def _stop() -> None:
        return None

    return SimpleNamespace(
        stream_client=sc,
        stop=_stop,
        get_status=None,
        handle_callback=None,
    )


class TestServerLoad:
    @pytest.mark.parametrize("load_server", ["wecom", "qq", "dingtalk"], indirect=True)
    def test_plugin_registered(self, load_server) -> None:
        # 模块名形如 channel_wecom_server_test → 渠道名 = 去掉前后缀
        channel = load_server.__name__.replace("_server_test", "")
        assert load_server.plugin.name == channel

class TestWeComServer:
    @pytest.mark.parametrize("load_server", ["wecom"], indirect=True)
    async def test_on_load_constructs_adapter(self, load_server, monkeypatch) -> None:
        monkeypatch.setattr(
            load_server.plugin,
            "get_config",
            lambda: {"corp_id": "ww1", "agent_id": "2", "secret": "s", "token": "t", "encoding_aes_key": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"},
        )
        await load_server._on_load({})
        # 适配器已构造的公共观察面：发送工具报"未连接"而非"未初始化"
        r = await load_server.wecom_send_message("u1", "hi")
        assert "not initialized" not in r["error"]
        assert "not connected" in r["error"]

    @pytest.mark.parametrize("load_server", ["wecom"], indirect=True)
    async def test_on_unload_stops_adapter(self, load_server) -> None:
        stopped = []

        async def _stop():
            stopped.append(1)

        load_server._adapter = SimpleNamespace(stop=_stop)
        await load_server._on_unload({})
        assert stopped == [1]
        # 卸载后的公共观察面：发送工具回到"未初始化"哨兵值
        r = await load_server.wecom_send_message("u1", "hi")
        assert r == {"error": "WeCom adapter not initialized"}

    @pytest.mark.parametrize("load_server", ["wecom"], indirect=True)
    async def test_on_unload_no_adapter(self, load_server) -> None:
        load_server._adapter = None
        await load_server._on_unload({})  # 不抛

    @pytest.mark.parametrize("load_server", ["wecom"], indirect=True)
    async def test_send_message_not_initialized(self, load_server) -> None:
        load_server._adapter = None
        r = await load_server.wecom_send_message("u1", "hi")
        assert r == {"error": "WeCom adapter not initialized"}

    @pytest.mark.parametrize("load_server", ["wecom"], indirect=True)
    async def test_send_message_stream_client_missing(self, load_server) -> None:
        load_server._adapter = SimpleNamespace(stream_client=None)
        r = await load_server.wecom_send_message("u1", "hi")
        assert r == {"error": "WeCom adapter not initialized"}

    @pytest.mark.parametrize("load_server", ["wecom"], indirect=True)
    async def test_send_message_not_connected(self, load_server) -> None:
        load_server._adapter = _fake_adapter(SimpleNamespace(_session=None))
        r = await load_server.wecom_send_message("u1", "hi")
        assert "not connected" in r["error"]

    @pytest.mark.parametrize("load_server", ["wecom"], indirect=True)
    async def test_send_message_success(self, load_server) -> None:
        import asyncio

        async def _send(*_a, **_k):
            return {"ok": True}

        load_server._adapter = _fake_adapter(SimpleNamespace(_session=object(), send_message=_send))
        r = await load_server.wecom_send_message("u1", "hi")
        assert r == {"ok": True}

    @pytest.mark.parametrize("load_server", ["wecom"], indirect=True)
    async def test_handle_callback_not_initialized(self, load_server) -> None:
        load_server._adapter = None
        r = await load_server.wecom_handle_callback("t", "n", "s", "b")
        assert r == {"error": "WeCom adapter not initialized"}

    @pytest.mark.parametrize("load_server", ["wecom"], indirect=True)
    async def test_handle_callback_success(self, load_server) -> None:
        import asyncio

        async def _cb(*_a, **_k):
            return "decrypted-msg"

        load_server._adapter = _fake_adapter()
        load_server._adapter.handle_callback = _cb
        r = await load_server.wecom_handle_callback("t", "n", "s", "b")
        assert r == {"decrypted": "decrypted-msg"}

    @pytest.mark.parametrize("load_server", ["wecom"], indirect=True)
    async def test_get_status_not_initialized(self, load_server) -> None:
        load_server._adapter = None
        r = await load_server.wecom_get_status()
        assert r == {"type": "wecom", "connected": False, "healthy": False}

    @pytest.mark.parametrize("load_server", ["wecom"], indirect=True)
    async def test_get_status_success(self, load_server) -> None:
        load_server._adapter = _fake_adapter()
        load_server._adapter.get_status = lambda: {"type": "wecom", "connected": True, "healthy": True}
        r = await load_server.wecom_get_status()
        assert r["connected"] is True


class TestQQServer:
    @pytest.mark.parametrize("load_server", ["qq"], indirect=True)
    async def test_on_load_constructs_adapter(self, load_server, monkeypatch) -> None:
        monkeypatch.setattr(load_server.plugin, "get_config", lambda: {})
        await load_server._on_load({})
        # 适配器已构造的公共观察面：发送工具报"未连接"而非"未初始化"
        r = await load_server.qq_send_message(1, "hi")
        assert "not initialized" not in r["error"]
        assert "not connected" in r["error"]

    @pytest.mark.parametrize("load_server", ["qq"], indirect=True)
    async def test_on_unload_stops_adapter(self, load_server) -> None:
        stopped = []

        async def _stop():
            stopped.append(1)

        load_server._adapter = SimpleNamespace(stop=_stop)
        await load_server._on_unload({})
        assert stopped == [1]
        # 卸载后的公共观察面：发送工具回到"未初始化"哨兵值
        r = await load_server.qq_send_message(1, "hi")
        assert r == {"error": "QQ adapter not initialized"}

    @pytest.mark.parametrize("load_server", ["qq"], indirect=True)
    async def test_send_message_not_initialized(self, load_server) -> None:
        load_server._adapter = None
        r = await load_server.qq_send_message(1, "hi")
        assert r == {"error": "QQ adapter not initialized"}

    @pytest.mark.parametrize("load_server", ["qq"], indirect=True)
    async def test_send_message_not_connected(self, load_server) -> None:
        load_server._adapter = _fake_adapter(SimpleNamespace(_session=None))
        r = await load_server.qq_send_message(1, "hi")
        assert "not connected" in r["error"]

    @pytest.mark.parametrize("load_server", ["qq"], indirect=True)
    async def test_send_message_success(self, load_server) -> None:
        import asyncio

        async def _send(*_a, **_k):
            return {"ok": True}

        load_server._adapter = _fake_adapter(SimpleNamespace(_session=object(), send_message=_send))
        r = await load_server.qq_send_message(1, "hi")
        assert r == {"ok": True}

    @pytest.mark.parametrize("load_server", ["qq"], indirect=True)
    async def test_get_status_not_initialized(self, load_server) -> None:
        load_server._adapter = None
        r = await load_server.qq_get_status()
        assert r == {"type": "qq", "connected": False, "healthy": False}

    @pytest.mark.parametrize("load_server", ["qq"], indirect=True)
    async def test_get_status_success(self, load_server) -> None:
        load_server._adapter = _fake_adapter()
        load_server._adapter.get_status = lambda: {"type": "qq", "connected": True, "healthy": True}
        r = await load_server.qq_get_status()
        assert r["connected"] is True


class TestDingTalkServer:
    @pytest.mark.parametrize("load_server", ["dingtalk"], indirect=True)
    async def test_on_load_constructs_adapter(self, load_server, monkeypatch) -> None:
        monkeypatch.setattr(load_server.plugin, "get_config", lambda: {"client_id": "a", "client_secret": "b"})
        await load_server._on_load({})
        # 适配器已构造的公共观察面：发送工具报"未连接"而非"未初始化"
        r = await load_server.dingtalk_send_message("u1", "hi")
        assert "not initialized" not in r["error"]
        assert "not connected" in r["error"]

    @pytest.mark.parametrize("load_server", ["dingtalk"], indirect=True)
    async def test_on_unload_stops_adapter(self, load_server) -> None:
        stopped = []

        async def _stop():
            stopped.append(1)

        load_server._adapter = SimpleNamespace(stop=_stop)
        await load_server._on_unload({})
        assert stopped == [1]
        # 卸载后的公共观察面：发送工具回到"未初始化"哨兵值
        r = await load_server.dingtalk_send_message("u1", "hi")
        assert r == {"error": "DingTalk adapter not initialized"}

    @pytest.mark.parametrize("load_server", ["dingtalk"], indirect=True)
    async def test_send_message_not_initialized(self, load_server) -> None:
        load_server._adapter = None
        r = await load_server.dingtalk_send_message("u1", "hi")
        assert r == {"error": "DingTalk adapter not initialized"}

    @pytest.mark.parametrize("load_server", ["dingtalk"], indirect=True)
    async def test_send_message_not_connected(self, load_server) -> None:
        load_server._adapter = _fake_adapter(SimpleNamespace(_session=None))
        r = await load_server.dingtalk_send_message("u1", "hi")
        assert "not connected" in r["error"]

    @pytest.mark.parametrize("load_server", ["dingtalk"], indirect=True)
    async def test_send_message_success(self, load_server) -> None:
        import asyncio

        async def _send(*_a, **_k):
            return {"ok": True}

        load_server._adapter = _fake_adapter(SimpleNamespace(_session=object(), send_message=_send))
        r = await load_server.dingtalk_send_message("u1", "hi")
        assert r == {"ok": True}

    @pytest.mark.parametrize("load_server", ["dingtalk"], indirect=True)
    async def test_get_status_not_initialized(self, load_server) -> None:
        load_server._adapter = None
        r = await load_server.dingtalk_get_status()
        assert r == {"type": "dingtalk", "connected": False, "healthy": False}

    @pytest.mark.parametrize("load_server", ["dingtalk"], indirect=True)
    async def test_get_status_success(self, load_server) -> None:
        load_server._adapter = _fake_adapter()
        load_server._adapter.get_status = lambda: {"type": "dingtalk", "connected": True, "healthy": True}
        r = await load_server.dingtalk_get_status()
        assert r["connected"] is True
