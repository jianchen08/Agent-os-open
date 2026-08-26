# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""environment_lifecycle server.py（MCP 接口适配层）单元测试。

覆盖：懒构建单例缓存与配置注入、on_load 预热、on_unload 清缓存、
execute 工具入口（PluginResult → state_updates 序列化 + error 透传）。

server.py 以唯一模块名动态加载（importlib 显式路径），加载前逐出裸名
``plugin`` 防兄弟插件目录串扰；``plugin.get_config`` 为 SDK 外部依赖，
以 monkeypatch 注入配置。

[来源: plugins/shared/pipeline/input/environment_lifecycle/server.py]
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SERVER_PATH = Path(__file__).resolve().parent / "server.py"


def _load_server() -> ModuleType:
    """唯一名动态加载 server.py（每次新建，隔离模块级状态）。"""
    name = "_env_lc_server_ut"
    sys.modules.pop(name, None)
    sys.modules.pop("plugin", None)
    spec = importlib.util.spec_from_file_location(name, _SERVER_PATH)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── 单例与生命周期 ───────────────────────────────────────────


def test_get_instance_cached_and_config_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _load_server()
    monkeypatch.setattr(server.plugin, "get_config", lambda: {"priority": 9})
    first = server.get_instance()
    second = server.get_instance()
    assert first is second
    assert first.priority == 9


def test_on_load_preheats_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _load_server()
    monkeypatch.setattr(server.plugin, "get_config", lambda: {})
    _run(server._on_load({}))
    assert server.get_instance() is not None


def test_on_unload_clears_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _load_server()
    monkeypatch.setattr(server.plugin, "get_config", lambda: {})
    first = server.get_instance()
    _run(server._on_unload({}))
    second = server.get_instance()
    assert first is not second


# ── execute 工具入口 ─────────────────────────────────────────


def test_execute_serializes_state_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _load_server()
    monkeypatch.setattr(server.plugin, "get_config", lambda: {})
    captured: dict = {}

    class _FakeResult:
        state_updates: dict[str, Any] = {"environment_basis": {"level": "isolated", "resolved": True}}
        error = None

    async def _fake_execute(ctx: Any) -> _FakeResult:
        captured["ctx"] = ctx
        return _FakeResult()

    monkeypatch.setattr(server.get_instance(), "execute", _fake_execute)
    data = _run(server.execute({"current_phase": "init"}))
    assert data == {
        "state_updates": {"environment_basis": {"level": "isolated", "resolved": True}},
        "error": None,
    }
    # 配置直通实例 execute
    assert captured["ctx"].state["current_phase"] == "init"


def test_execute_serializes_error(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _load_server()
    monkeypatch.setattr(server.plugin, "get_config", lambda: {})

    class _FakeResult:
        state_updates: dict[str, Any] = {}
        error = RuntimeError("boom")

    async def _fake_execute(ctx: Any) -> _FakeResult:
        return _FakeResult()

    monkeypatch.setattr(server.get_instance(), "execute", _fake_execute)
    data = _run(server.execute({"current_phase": "exit"}))
    assert data == {"state_updates": {}, "error": "boom"}


def test_execute_passes_config_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _load_server()
    monkeypatch.setattr(server.plugin, "get_config", lambda: {})
    captured: dict = {}

    class _FakeResult:
        state_updates: dict[str, Any] = {}
        error = None

    async def _fake_execute(ctx: Any) -> _FakeResult:
        captured["config"] = ctx.config
        return _FakeResult()

    monkeypatch.setattr(server.get_instance(), "execute", _fake_execute)
    _run(server.execute({"current_phase": "init"}, config={"config_path": "x.yaml"}))
    assert captured["config"] == {"config_path": "x.yaml"}
