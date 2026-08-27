# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""channel_feishu server.py 适配层测试（A5.2 补）。

server.py 是纯接口适配层：@plugin.on_load 建 FeishuAdapter、@plugin.tool 暴露
feishu.send_message / feishu.send_card / feishu.get_status。用 importlib
独立加载 + 假 adapter 注入覆盖全部分支，不触发真实网络 I/O。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_FEISHU_DIR = Path(__file__).resolve().parents[2] / "plugins" / "shared" / "system" / "channel_feishu"
_CC_DIR = _FEISHU_DIR.parent / "channel_common"

# 与 server.py 顶层 import 冲突的同名平铺模块
_EVICT = {
    "server", "adapter", "stream_client", "card_builder",
    "input_adapter", "output_adapter", "base_combo_adapter", "pipeline_types",
}


@pytest.fixture()
def load_feishu_server():
    """加载 feishu server 模块，teardown 清理 sys.path 与 sys.modules。"""
    saved_path = list(sys.path)
    saved_modules = {m: sys.modules[m] for m in _EVICT if m in sys.modules}
    mod_name = "channel_feishu_server_test"
    try:
        sys.path.insert(0, str(_FEISHU_DIR))
        # 移除可能已被其他渠道测试加入的 channel_common，让 server.py 自身
        # 执行 append 接线分支（组合跑时该行确定性覆盖）
        while str(_CC_DIR) in sys.path:
            sys.path.remove(str(_CC_DIR))
        for m in _EVICT:
            sys.modules.pop(m, None)
        sys.modules.pop(mod_name, None)
        spec = importlib.util.spec_from_file_location(mod_name, _FEISHU_DIR / "server.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path[:] = saved_path
        sys.modules.pop(mod_name, None)
        for name, mod in saved_modules.items():
            sys.modules[name] = mod


def _fake_stream_client(session: Any = None) -> SimpleNamespace:
    return SimpleNamespace(_session=session, send_message=None, send_card=None)


def _fake_adapter(stream_client: Any = None) -> SimpleNamespace:
    sc = stream_client if stream_client is not None else _fake_stream_client()

    async def _stop() -> None:
        return None

    return SimpleNamespace(stream_client=sc, stop=_stop, get_status=None)


def _fake_adapter_no_stream_client() -> SimpleNamespace:
    """构造 stream_client 为 None 的假 adapter（触发 not initialized 分支）。"""

    async def _stop() -> None:
        return None

    return SimpleNamespace(stream_client=None, stop=_stop, get_status=None)


class TestFeishuServerLoad:
    """server 模块加载与工具注册。"""

    def test_plugin_registered(self, load_feishu_server) -> None:
        assert load_feishu_server.plugin.name == "channel_feishu"

    def test_load_with_common_already_in_path(self) -> None:
        # channel_common 已在 sys.path 时，server.py 的 append 守卫走 False 分支
        saved_path = list(sys.path)
        saved_modules = {m: sys.modules[m] for m in _EVICT if m in sys.modules}
        mod_name = "channel_feishu_server_test"
        try:
            sys.path.insert(0, str(_FEISHU_DIR))
            if str(_CC_DIR) not in sys.path:
                sys.path.append(str(_CC_DIR))
            for m in _EVICT:
                sys.modules.pop(m, None)
            sys.modules.pop(mod_name, None)
            spec = importlib.util.spec_from_file_location(mod_name, _FEISHU_DIR / "server.py")
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
            assert module.plugin.name == "channel_feishu"
        finally:
            sys.path[:] = saved_path
            sys.modules.pop(mod_name, None)
            for name, mod in saved_modules.items():
                sys.modules[name] = mod


class TestFeishuServerLifecycle:
    """on_load / on_unload 生命周期。"""

    @pytest.mark.asyncio
    async def test_on_load_constructs_adapter(self, load_feishu_server, monkeypatch) -> None:
        monkeypatch.setattr(
            load_feishu_server.plugin,
            "get_config",
            lambda: {"app_id": "cli_x", "app_secret": "secret"},
        )
        await load_feishu_server._on_load({})
        # 适配器已构造的公共观察面：发送工具报"未连接"而非"未初始化"
        r = await load_feishu_server.feishu_send_message("u1", "hi")
        assert "not initialized" not in r["error"]
        assert "not connected" in r["error"]

    @pytest.mark.asyncio
    async def test_on_unload_stops_adapter(self, load_feishu_server) -> None:
        stopped: list[str] = []

        async def _stop() -> None:
            stopped.append("stopped")

        load_feishu_server._adapter = SimpleNamespace(stop=_stop)
        await load_feishu_server._on_unload({})
        assert stopped == ["stopped"]
        # 卸载后的公共观察面：发送工具回到"未初始化"哨兵值
        r = await load_feishu_server.feishu_send_message("u1", "hi")
        assert r == {"error": "Feishu adapter not initialized"}

    @pytest.mark.asyncio
    async def test_on_unload_no_adapter(self, load_feishu_server) -> None:
        load_feishu_server._adapter = None
        await load_feishu_server._on_unload({})  # 不抛


class TestFeishuServerSendMessage:
    """feishu.send_message 工具分支。"""

    @pytest.mark.asyncio
    async def test_not_initialized(self, load_feishu_server) -> None:
        load_feishu_server._adapter = None
        r = await load_feishu_server.feishu_send_message("u1", "hi")
        assert r == {"error": "Feishu adapter not initialized"}

    @pytest.mark.asyncio
    async def test_stream_client_missing(self, load_feishu_server) -> None:
        load_feishu_server._adapter = _fake_adapter_no_stream_client()
        r = await load_feishu_server.feishu_send_message("u1", "hi")
        assert r == {"error": "Feishu adapter not initialized"}

    @pytest.mark.asyncio
    async def test_not_connected(self, load_feishu_server) -> None:
        load_feishu_server._adapter = _fake_adapter(_fake_stream_client(session=None))
        r = await load_feishu_server.feishu_send_message("u1", "hi")
        assert r == {"error": "Feishu stream client not connected"}

    @pytest.mark.asyncio
    async def test_success(self, load_feishu_server) -> None:
        async def _send(*_a: Any, **_k: Any) -> dict[str, Any]:
            return {"code": 0, "msg": "ok"}

        load_feishu_server._adapter = _fake_adapter(
            _fake_stream_client(session=object())
        )
        load_feishu_server._adapter.stream_client.send_message = _send
        r = await load_feishu_server.feishu_send_message("u1", "hi", "text")
        assert r == {"code": 0, "msg": "ok"}


class TestFeishuServerSendCard:
    """feishu.send_card 工具分支。"""

    @pytest.mark.asyncio
    async def test_not_initialized(self, load_feishu_server) -> None:
        load_feishu_server._adapter = None
        r = await load_feishu_server.feishu_send_card("u1", {"elements": []})
        assert r == {"error": "Feishu adapter not initialized"}

    @pytest.mark.asyncio
    async def test_not_connected(self, load_feishu_server) -> None:
        load_feishu_server._adapter = _fake_adapter(_fake_stream_client(session=None))
        r = await load_feishu_server.feishu_send_card("u1", {"elements": []})
        assert r == {"error": "Feishu stream client not connected"}

    @pytest.mark.asyncio
    async def test_success(self, load_feishu_server) -> None:
        async def _send_card(*_a: Any, **_k: Any) -> dict[str, Any]:
            return {"code": 0, "msg": "ok"}

        load_feishu_server._adapter = _fake_adapter(
            _fake_stream_client(session=object())
        )
        load_feishu_server._adapter.stream_client.send_card = _send_card
        r = await load_feishu_server.feishu_send_card("u1", {"elements": []})
        assert r == {"code": 0, "msg": "ok"}


class TestFeishuServerGetStatus:
    """feishu.get_status 工具分支。"""

    @pytest.mark.asyncio
    async def test_not_initialized(self, load_feishu_server) -> None:
        load_feishu_server._adapter = None
        r = await load_feishu_server.feishu_get_status()
        assert r == {"type": "feishu", "connected": False, "healthy": False}

    @pytest.mark.asyncio
    async def test_success(self, load_feishu_server) -> None:
        load_feishu_server._adapter = _fake_adapter()
        load_feishu_server._adapter.get_status = lambda: {
            "type": "feishu", "connected": True, "healthy": True,
        }
        r = await load_feishu_server.feishu_get_status()
        assert r["connected"] is True
        assert r["healthy"] is True
