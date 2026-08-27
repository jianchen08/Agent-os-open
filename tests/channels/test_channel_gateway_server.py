# @feature: FP-0.2.七 路由收敛 | @vision: V3 可嵌入 | @ci: python-coverage
"""channel_gateway server.py 工具面测试。

server.py 是纯接口适配层：@plugin.tool 暴露 gateway.handle_message /
gateway.send_response / gateway.get_adapters。本文件锁定的行为契约：
网关返回的真实结果（含失败值）必须原样到达工具调用方，
不得被适配层覆盖成 handled/sent 假成功。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

_SYSTEM_DIR = Path(__file__).resolve().parents[2] / "plugins" / "shared" / "system"
_GW_DIR = _SYSTEM_DIR / "channel_gateway"

# 与渠道各目录平铺模块名冲突的缓存项（跨测试文件隔离）
_EVICT = {
    "channel_gateway", "message_normalizer", "session_bridge",
    "unified_types", "server",
}


@pytest.fixture()
def gw_server():
    """独立加载 channel_gateway/server.py，teardown 还原 sys.path/sys.modules。"""
    saved_path = list(sys.path)
    saved_modules = {m: sys.modules[m] for m in _EVICT if m in sys.modules}
    try:
        sys.path.insert(0, str(_GW_DIR))
        for m in _EVICT:
            sys.modules.pop(m, None)
        mod_name = "channel_gateway_server_test"
        sys.modules.pop(mod_name, None)
        spec = importlib.util.spec_from_file_location(
            mod_name, _GW_DIR / "server.py"
        )
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


class TestGatewayServerTools:
    def test_plugin_name(self, gw_server) -> None:
        assert gw_server.plugin.name == "channel_gateway"

    @pytest.mark.asyncio
    async def test_handle_message_not_initialized(self, gw_server) -> None:
        gw_server._gateway = None
        result = await gw_server.gateway_handle_message("feishu", {"x": 1})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_handle_message_failure_value_reaches_caller(self, gw_server) -> None:
        """网关返回的失败值必须原样到达工具调用方，不得改写成 handled=True。"""
        failure: dict[str, Any] = {"handled": False, "error": "pipeline not wired"}

        class _FakeGateway:
            async def handle_message(self, channel_type: str, raw: dict[str, Any]) -> dict[str, Any]:
                return failure

        gw_server._gateway = _FakeGateway()
        result = await gw_server.gateway_handle_message("feishu", {"raw": True})

        assert result["handled"] is False
        assert result["error"] == "pipeline not wired"

    @pytest.mark.asyncio
    async def test_handle_message_success_reaches_caller(self, gw_server) -> None:
        success = {"handled": True, "session_id": "s-1"}

        class _FakeGateway:
            async def handle_message(self, channel_type: str, raw: dict[str, Any]) -> dict[str, Any]:
                return dict(success)

        gw_server._gateway = _FakeGateway()
        result = await gw_server.gateway_handle_message("dingtalk", {"raw": True})

        assert result["handled"] is True
        assert result["session_id"] == "s-1"

    @pytest.mark.asyncio
    async def test_send_response_failure_value_reaches_caller(self, gw_server) -> None:
        """发送失败值必须原样到达工具调用方，不得改写成 sent=True。"""
        calls: list[Any] = []

        class _FakeGateway:
            async def send_response(self, response: Any) -> dict[str, Any]:
                calls.append(response)
                return {"sent": False, "error": "no adapter for channel feishu"}

        gw_server._gateway = _FakeGateway()
        result = await gw_server.gateway_send_response(
            channel_type="feishu", content="hi", message_id="m1"
        )

        assert result["sent"] is False
        assert "no adapter" in result["error"]

    @pytest.mark.asyncio
    async def test_send_response_not_initialized(self, gw_server) -> None:
        gw_server._gateway = None
        result = await gw_server.gateway_send_response(
            channel_type="feishu", content="hi"
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_get_adapters_not_initialized(self, gw_server) -> None:
        gw_server._gateway = None
        result = await gw_server.gateway_get_adapters()
        assert result["adapters"] == []
        assert "error" in result

    @pytest.mark.asyncio
    async def test_get_adapters_lists_registered(self, gw_server) -> None:
        gateway = AsyncMock(spec=["_adapters"])
        gateway._adapters = {"feishu": object(), "dingtalk": object()}
        gw_server._gateway = gateway
        result = await gw_server.gateway_get_adapters()
        assert sorted(result["adapters"]) == ["dingtalk", "feishu"]
