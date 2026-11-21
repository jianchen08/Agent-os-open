# @feature: FP-0.2.可观测性 dsh_adapter 插件（工具面进入工具） | @ci: python-coverage
"""dsh.tool.list / dsh.tool.call 内核工具的分支测试（桥打桩，不启动真桥）。

口径：list 投影 name/description/parameters（坏条目跳过）；call 空名
fail-visible、其余信封原样透传（bridge.call_tool 的三级信封就是契约）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

pytestmark = pytest.mark.unit


def _load_server() -> Any:
    name = "dsh_adapter_server_toolface_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def srv() -> Any:
    return _load_server()


class _FakeBridge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    async def list_tools(self) -> list[dict[str, Any]]:
        self.calls.append(("list_tools", "", None))
        return [
            {"name": "calculator", "description": "算术", "parameters": {"expression": {"type": "string"}}},
            {"name": "", "description": "坏条目（无名）"},
            "not-a-dict",
            {"name": "now", "description": "时间"},
        ]

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("call_tool", name, args))
        return {"success": True, "data": 42, "error": None, "duration_ms": 7}


@pytest.fixture
def fake_bridge(srv: Any, monkeypatch: pytest.MonkeyPatch) -> _FakeBridge:
    bridge = _FakeBridge()
    monkeypatch.setattr(srv, "_MANAGER", object())
    import bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "_BRIDGE", bridge, raising=False)
    monkeypatch.setattr(bridge_mod, "get_bridge", lambda: bridge, raising=False)
    return bridge


def test_tool_list_projects_entries_and_skips_bad(srv: Any, fake_bridge: _FakeBridge) -> None:
    out = asyncio.run(srv.dsh_tool_list())
    assert out["success"] is True
    assert out["error"] is None
    names = [t["name"] for t in out["data"]]
    assert names == ["calculator", "now"]
    calc = out["data"][0]
    assert calc["parameters"] == {"expression": {"type": "string"}}
    assert ("list_tools", "", None) in fake_bridge.calls


def test_tool_call_envelope_passthrough(srv: Any, fake_bridge: _FakeBridge) -> None:
    out = asyncio.run(srv.dsh_tool_call("calculator", {"expression": "1+1"}))
    assert out == {"success": True, "data": 42, "error": None, "duration_ms": 7}
    assert ("call_tool", "calculator", {"expression": "1+1"}) in fake_bridge.calls


def test_tool_call_blank_name_fail_visible(srv: Any, fake_bridge: _FakeBridge) -> None:
    out = asyncio.run(srv.dsh_tool_call("   "))
    assert out["success"] is False
    assert "dsh.tool.list" in out["error"]
    assert not any(kind == "call_tool" for kind, _, _ in fake_bridge.calls)


def test_tool_call_defaults_args_to_empty(srv: Any, fake_bridge: _FakeBridge) -> None:
    asyncio.run(srv.dsh_tool_call("now"))
    assert ("call_tool", "now", {}) in fake_bridge.calls
