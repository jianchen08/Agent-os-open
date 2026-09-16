# @feature: FP-T07 llm api | @ci: python-coverage
"""llm_core server.py execute 工具函数测试——state_updates 包装与路由信号。

契约：server.execute 把 LLMCore.execute 的返回 dict 包成
``{"state_updates": <dict>}`` 供内核反序列化为 PluginResult；非 dict 返回
（PluginResult 形态）时透传 state_updates/route_signal/skip_remaining。

加载：server.py 经 importlib 唯一模块名装载（裸名 ``server`` 会被兄弟插件
目录串扰）；``plugin`` 裸名在加载前逐出、加载后还原（本仓逐出纪律：还原到
原值而非留空，避免后续测试重解析出身份分裂的新实例）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent

_MOD_NAME = "llm_core_server_execute_under_test"


def _load_server() -> Any:
    """加载 llm_core/server.py（唯一模块名，进程内缓存）。"""
    if _MOD_NAME in sys.modules:
        return sys.modules[_MOD_NAME]
    # 逐出裸名 plugin：server.py 内部 `from plugin import LLMCore` 须命中
    # 本目录 plugin.py（兄弟插件目录同名串扰）
    _saved_plugin = sys.modules.pop("plugin", None)
    try:
        spec = importlib.util.spec_from_file_location(_MOD_NAME, _PLUGIN_DIR / "server.py")
        assert spec is not None and spec.loader is not None, "cannot load llm_core server.py"
        mod = importlib.util.module_from_spec(spec)
        sys.modules[_MOD_NAME] = mod
        spec.loader.exec_module(mod)
    finally:
        # 还原 plugin 槽位到原值（逐出到空会让后续测试重解析出身份分裂的新实例）
        if _saved_plugin is not None:
            sys.modules["plugin"] = _saved_plugin
        else:
            sys.modules.pop("plugin", None)
    return mod


class _FakeInstance:
    """伪 LLMCore 单例：execute 返回预设结果。"""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.calls: list[Any] = []

    async def execute(self, ctx: Any) -> Any:
        self.calls.append(ctx)
        return self._result


async def test_execute_dict_result_wrapped_in_state_updates(monkeypatch: Any) -> None:
    """dict 返回 → 包成 {"state_updates": <dict>}（内核 PluginResult 反序列化形态）。"""
    server_mod = _load_server()
    fake = _FakeInstance({"raw_result": "ok", "llm_model": "m"})
    monkeypatch.setattr(server_mod, "get_instance", lambda: fake)

    out = await server_mod.execute({"messages": [{"role": "user", "content": "hi"}]}, {})
    assert out == {"state_updates": {"raw_result": "ok", "llm_model": "m"}}
    # 状态经 create_initial_state 合并（含默认字段）
    assert fake.calls[0].state["messages"] == [{"role": "user", "content": "hi"}]
    assert fake.calls[0].state["raw_result"] is None  # 默认字段存在


async def test_execute_plugin_result_passthrough(monkeypatch: Any) -> None:
    """非 dict 返回（PluginResult 形态）→ 透传 state_updates，不产出路由键。"""
    server_mod = _load_server()
    result = SimpleNamespace(
        state_updates={"raw_result": "x"},
        skip_remaining=False,
    )
    fake = _FakeInstance(result)
    monkeypatch.setattr(server_mod, "get_instance", lambda: fake)

    out = await server_mod.execute({"messages": []}, {})
    assert out == {"state_updates": {"raw_result": "x"}}
    assert "route_signal" not in out


async def test_execute_plugin_result_with_skip_remaining(monkeypatch: Any) -> None:
    """skip_remaining=True → 透传。"""
    server_mod = _load_server()
    result = SimpleNamespace(
        state_updates={"raw_result": "y"},
        skip_remaining=True,
    )
    fake = _FakeInstance(result)
    monkeypatch.setattr(server_mod, "get_instance", lambda: fake)

    out = await server_mod.execute({"messages": []}, {"temperature": 0.5})
    assert out == {"state_updates": {"raw_result": "y"}, "skip_remaining": True}


# ── 观察出口桥接：frontend/metrics capability → ctx._services 注入 ────────
class _FakeCapability:
    """capability 句柄桩（记录调用并回放 ok）。"""

    async def call(self, method: str, params: Any = None, timeout: Any = None) -> Any:
        return {"success": True, "method": method, "params": params}


class _CapabilityPlugin:
    """plugin 桩：按声明名集决定哪些 capability 可取。"""

    def __init__(self, available: set[str]) -> None:
        self._available = available

    def get_capability(self, name: str) -> Any:
        if name not in self._available:
            raise KeyError(name)
        return _FakeCapability()


async def test_execute_injects_frontend_and_metrics_services(monkeypatch: Any) -> None:
    """内核声明两 capability → ctx._services 同时注入 frontend 与 metrics 句柄。"""
    server_mod = _load_server()
    fake = _FakeInstance({"raw_result": "ok"})
    monkeypatch.setattr(server_mod, "get_instance", lambda: fake)
    monkeypatch.setattr(
        server_mod, "plugin", _CapabilityPlugin({"frontend", "metrics"})
    )

    await server_mod.execute({"messages": []}, {})

    svc = fake.calls[0]._services
    assert "frontend" in svc and "metrics" in svc


async def test_execute_skips_service_injection_when_capability_absent(
    monkeypatch: Any,
) -> None:
    """旧内核未声明 capability → from_plugin 返回 None，不注入（插件侧静默跳过出口）。"""
    server_mod = _load_server()
    fake = _FakeInstance({"raw_result": "ok"})
    monkeypatch.setattr(server_mod, "get_instance", lambda: fake)
    monkeypatch.setattr(server_mod, "plugin", _CapabilityPlugin(set()))

    await server_mod.execute({"messages": []}, {})

    svc = fake.calls[0]._services
    assert "frontend" not in svc
    assert "metrics" not in svc


async def test_execute_injects_only_present_capability(monkeypatch: Any) -> None:
    """只声明 metrics 时仅注入 metrics（两出口相互独立，不因缺一而全废）。"""
    server_mod = _load_server()
    fake = _FakeInstance({"raw_result": "ok"})
    monkeypatch.setattr(server_mod, "get_instance", lambda: fake)
    monkeypatch.setattr(server_mod, "plugin", _CapabilityPlugin({"frontend"}))

    await server_mod.execute({"messages": []}, {})

    svc = fake.calls[0]._services
    assert "frontend" in svc
    assert "metrics" not in svc
