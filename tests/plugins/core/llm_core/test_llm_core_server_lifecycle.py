# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""llm_core server.py 生命周期测试（统一路径接线）。

on_load 向 plugin.py 注入 capability caller（LLM 调用通道唯一事实源 =
llm_service：tool-executor.invoke → llm.complete_stream），on_unload 清空。
本文件只验证 server 与 plugin 的接线（caller 已注入、lambda 经
get_capability("tool-executor").call 转发、卸载后清空）；调用语义本身由
test_llm_core_partial_persist / test_llm_core_thinking_strength 覆盖。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = (
    Path(__file__).resolve().parents[4]
    / "plugins" / "shared" / "pipeline" / "core" / "llm_core"
)


def _load_plugin_module() -> Any:
    """加载 llm_core 的 plugin.py（裸名 ``plugin``，与 server.py 内部 import 同解析）。"""
    if "plugin" in sys.modules:
        del sys.modules["plugin"]
    spec = importlib.util.spec_from_file_location("plugin", _PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None, "cannot load llm_core plugin.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plugin"] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_server_module() -> Any:
    """按显式路径加载 llm_core 的 server.py（唯一模块名隔离同名 server.py）。"""
    mod_name = "llm_core_server_lifecycle_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None, "cannot load llm_core server.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeToolExecutor:
    """伪 tool-executor capability 句柄：记录 call 的 method/params。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        self.calls.append((method, params))
        # tool-executor.invoke 信封（内核 ToolExecutionResult 序列化形态）
        return {"success": True, "data": {"status": "ok"}}


def _inject_tool_executor(server_mod: Any, handle: _FakeToolExecutor) -> None:
    """覆盖 server 插件的 get_capability：只放行 tool-executor，其余照旧。"""
    original = server_mod.plugin.get_capability

    def _get(name: str) -> Any:
        if name == "tool-executor":
            return handle
        return original(name)

    server_mod.plugin.get_capability = _get  # type: ignore[method-assign]


async def test_on_load_injects_capability_caller() -> None:
    """on_load → _capability_caller 注入；经其调用转发到 tool-executor 能力。"""
    server_mod = _load_server_module()
    plugin_mod = _load_plugin_module()
    handle = _FakeToolExecutor()
    _inject_tool_executor(server_mod, handle)

    assert plugin_mod._capability_caller is None
    await server_mod._on_load({})
    assert plugin_mod._capability_caller is not None

    params = {"tool_name": "llm.complete_stream", "args": {"model": "m", "messages": []}}
    # caller 契约：capability 短名（SDK 句柄组装全名，传全名会双重前缀）
    result = await plugin_mod._capability_caller("invoke", params)
    assert result == {"success": True, "data": {"status": "ok"}}
    assert handle.calls == [("invoke", params)]


async def test_on_unload_clears_capability_caller() -> None:
    """on_unload → _capability_caller 清空（防止跨插件生命周期残留调用通道）。"""
    server_mod = _load_server_module()
    plugin_mod = _load_plugin_module()
    handle = _FakeToolExecutor()
    _inject_tool_executor(server_mod, handle)

    await server_mod._on_load({})
    assert plugin_mod._capability_caller is not None
    await server_mod._on_unload({})
    assert plugin_mod._capability_caller is None
