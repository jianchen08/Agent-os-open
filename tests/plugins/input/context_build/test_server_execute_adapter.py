# @feature: FP-0.2.二 管道插件服务接缝 | @ci: python-coverage
"""context_build server.py 执行适配层测试。

锁 server.execute 适配契约（业务逻辑归 plugin.py 自身测试）：
state/config 装配 → 插件 execute → 拆包扁平字典；on_load 预热单例。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = (
    Path(__file__).resolve().parents[4]
    / "plugins"
    / "shared"
    / "pipeline"
    / "input"
    / "context_build"
)


def _load_server() -> Any:
    """以唯一裸名装载 context_build/server.py（同 godot_context 缺口测试先例）。"""
    mod_name = "context_build_server_seam_test"
    spec = importlib.util.spec_from_file_location(mod_name, str(_PLUGIN_DIR / "server.py"))
    assert spec is not None, "Cannot load server.py"
    assert spec.loader is not None, "Cannot load server.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestExecuteAdapter:
    """server.execute 适配层：state/config 装配 → 插件 execute → 拆包（56/87-100）。

    契约：工具面返回**扁平字典**（state_updates/可选 skip_remaining），
    把 PluginResult 拆包成管道引擎可直接合并的形态；插件返回 dict 时原样直返。
    on_load 预热单例（56）——否则进程内首个工具调用才构造，启动期校验缺失。
    """

    def _fresh_server(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        """装载 server 并复位单例缓存（每用例独立预热）。"""
        server = _load_server()
        server.get_instance.cache_clear()
        return server

    def test_on_load_warms_singleton(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """on_load 预热单例（56）：装载后 get_instance 已有缓存，不再重新构造。"""
        server = self._fresh_server(monkeypatch)
        assert server.get_instance.cache_info().currsize == 0

        asyncio.run(server._on_load({}))

        assert server.get_instance.cache_info().currsize == 1
        warmed = server.get_instance()
        assert server.get_instance() is warmed, "预热后复用同一实例"

    def test_execute_returns_state_updates_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """正常路径：返回扁平 dict 且 state_updates 非空（上下文已构建，87-97）。"""
        server = self._fresh_server(monkeypatch)

        out = asyncio.run(server.execute(state={"task.id": "t-1"}))

        assert isinstance(out, dict)
        assert isinstance(out["state_updates"], dict)
        # 真实插件产出上下文键（system_prompt 是每次执行必写的键）
        assert "context.system_prompt" in out["state_updates"]

    def test_execute_merges_into_initial_state_contract(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """state 经 create_initial_state 装配：缺省字段补齐且不丢传入键（89-91）。"""
        server = self._fresh_server(monkeypatch)
        seen: dict[str, Any] = {}

        class _Probe:
            async def execute(self, ctx: Any) -> Any:
                seen.update(ctx.state)
                from agentos_plugin_sdk.pipeline_types import PluginResult

                return PluginResult(state_updates={"probe": True})

        monkeypatch.setattr(server, "get_instance", lambda: _Probe())

        out = asyncio.run(server.execute(state={"task.id": "t-9"}))

        assert seen["task.id"] == "t-9", "传入键保留"
        assert "iteration" in seen, "create_initial_state 补齐缺省字段"
        assert out == {"state_updates": {"probe": True}}

    def test_execute_propagates_skip_remaining_flag(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """skip_remaining=True → 拆包后带上该键（98-99）；False 时不写该键。"""
        server = self._fresh_server(monkeypatch)

        class _Skip:
            def __init__(self, skip: bool) -> None:
                self._skip = skip

            async def execute(self, ctx: Any) -> Any:
                from agentos_plugin_sdk.pipeline_types import PluginResult

                return PluginResult(state_updates={"k": 1}, skip_remaining=self._skip)

        monkeypatch.setattr(server, "get_instance", lambda: _Skip(True))
        out_skip = asyncio.run(server.execute(state={}))
        assert out_skip["skip_remaining"] is True
        assert out_skip["state_updates"] == {"k": 1}

        monkeypatch.setattr(server, "get_instance", lambda: _Skip(False))
        out_normal = asyncio.run(server.execute(state={}))
        assert "skip_remaining" not in out_normal, "False 不占键（默认语义）"

    def test_execute_passes_dict_result_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """插件返回 dict（core 插件形态）→ 原样直返，不做拆包（94-95）。"""
        server = self._fresh_server(monkeypatch)

        class _DictPlugin:
            async def execute(self, ctx: Any) -> dict[str, Any]:
                return {"state_updates": {"raw": True}, "custom": "kept"}

        monkeypatch.setattr(server, "get_instance", lambda: _DictPlugin())

        out = asyncio.run(server.execute(state={}))

        assert out == {"state_updates": {"raw": True}, "custom": "kept"}

    def test_execute_config_override_reaches_plugin_context(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """config 参数透传到 PluginContext.config（缺省为空字典）。"""
        server = self._fresh_server(monkeypatch)
        seen: list[dict[str, Any]] = []

        class _Probe:
            async def execute(self, ctx: Any) -> Any:
                seen.append(dict(ctx.config))
                from agentos_plugin_sdk.pipeline_types import PluginResult

                return PluginResult(state_updates={})

        monkeypatch.setattr(server, "get_instance", lambda: _Probe())

        asyncio.run(server.execute(state={}, config={"priority": 3}))
        asyncio.run(server.execute(state={}))

        assert seen[0] == {"priority": 3}
        assert seen[1] == {}, "config 缺省不写 None"
