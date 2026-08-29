# @feature: FP-0.2.〇 管道引擎 | @vision: V6 可即用 | @ci: python-coverage
"""multimodal_preprocessor server.py（MCP 接口适配层）单元测试。

覆盖：懒构建单例缓存、on_load 预热、on_unload 清缓存、execute 工具入口
（PluginResult → state_updates 序列化、dict 直通、skip_remaining
透传、create_initial_state 合并）。

server.py 以唯一模块名动态加载（importlib 显式路径），加载前逐出裸名
``plugin`` 防兄弟插件目录串扰；``plugin.get_config`` 为 SDK 外部依赖，
以 monkeypatch 注入配置。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SERVER_PATH = Path(__file__).resolve().parent / "server.py"


def _load_server() -> ModuleType:
    """唯一名动态加载 server.py（每次新建，隔离模块级状态）。"""
    name = "_mm_preprocessor_server_ut"
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
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── 单例与生命周期 ───────────────────────────────────────────


def test_get_instance_cached_and_config_injected(monkeypatch):
    server = _load_server()
    monkeypatch.setattr(server.plugin, "get_config", lambda: {"priority": 7})
    first = server.get_instance()
    second = server.get_instance()
    assert first is second
    assert first.priority == 7


def test_on_load_preheats_instance(monkeypatch):
    server = _load_server()
    monkeypatch.setattr(server.plugin, "get_config", lambda: {})
    _run(server._on_load({}))
    assert server.get_instance() is not None


def test_on_unload_clears_cache(monkeypatch):
    server = _load_server()
    monkeypatch.setattr(server.plugin, "get_config", lambda: {})
    first = server.get_instance()
    _run(server._on_unload({}))
    second = server.get_instance()
    assert first is not second


# ── execute 工具入口 ─────────────────────────────────────────


def test_execute_returns_state_updates(monkeypatch):
    server = _load_server()
    monkeypatch.setattr(server.plugin, "get_config", lambda: {})

    class _FakeResult:
        state_updates = {"multimodal_content": [{"type": "text", "text": "x"}], "has_multimodal": True}
        skip_remaining = False

    async def _fake_execute(ctx: Any) -> _FakeResult:
        return _FakeResult()

    monkeypatch.setattr(server.get_instance(), "execute", _fake_execute)
    data = _run(server.execute({"user_input": "hi"}))
    assert data == {"state_updates": {"multimodal_content": [{"type": "text", "text": "x"}], "has_multimodal": True}}


def test_execute_dict_result_passthrough(monkeypatch):
    server = _load_server()
    monkeypatch.setattr(server.plugin, "get_config", lambda: {})

    async def _fake_execute(ctx: Any) -> dict:
        return {"state_updates": {"k": 1}}

    monkeypatch.setattr(server.get_instance(), "execute", _fake_execute)
    data = _run(server.execute({"user_input": "hi"}))
    assert data == {"state_updates": {"k": 1}}


def test_execute_skip_remaining(monkeypatch):
    """OutputResult 形态 → 信封展开 state_updates/skip_remaining（RouteSignal 已退役）。"""
    server = _load_server()
    monkeypatch.setattr(server.plugin, "get_config", lambda: {})

    class _FakeResult:
        state_updates = {}
        skip_remaining = True

    async def _fake_execute(ctx: Any) -> _FakeResult:
        return _FakeResult()

    monkeypatch.setattr(server.get_instance(), "execute", _fake_execute)
    data = _run(server.execute({"user_input": "hi"}))
    assert data == {
        "state_updates": {},
        "skip_remaining": True,
    }


def test_execute_merges_state_via_create_initial_state(monkeypatch):
    server = _load_server()
    monkeypatch.setattr(server.plugin, "get_config", lambda: {})
    captured: dict = {}

    async def _fake_execute(ctx: Any) -> dict:
        captured["state"] = ctx.state
        return {"state_updates": {}}

    monkeypatch.setattr(server.get_instance(), "execute", _fake_execute)
    _run(server.execute({"user_input": "hi", "attachments": []}))
    # create_initial_state 补齐默认键，且用户输入保留
    assert captured["state"]["user_input"] == "hi"
    assert captured["state"]["attachments"] == []
    assert captured["state"]["iteration"] == 0
