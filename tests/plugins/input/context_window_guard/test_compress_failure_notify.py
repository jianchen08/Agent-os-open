# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""压缩彻底失败的前端透传测试（frontend.emit 通道）。

契约：压缩失败（compress_messages 异常或无有效收缩）时，经 frontend.emit
向前端推一次 compression_failed 事件；连续失败不重复推送（同一故障不刷
屏），压缩成功后复位——再次失败可再次推送。通道未注入时降级（不打断管线）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


def _load_cw_plugin() -> ModuleType:
    """唯一名动态加载 plugin.py（防兄弟插件 sys.modules 缓存串扰）。"""
    path = (
        Path(__file__).resolve().parents[4]
        / "plugins" / "shared" / "pipeline" / "input" / "context_window_guard"
        / "plugin.py"
    )
    name = "_cw_guard_plugin_notify_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


pytestmark = pytest.mark.unit


def _make_plugin_with_service(mod: ModuleType, compress_return: Any) -> tuple[Any, MagicMock]:
    """构造插件 + 注入失败/成功的压缩服务 mock，返回 (plugin, service)。"""
    plugin = mod.ContextWindowGuardPlugin(config={})
    service = MagicMock()
    service.setup = MagicMock()
    service.compress_messages = AsyncMock(return_value=compress_return)
    return plugin, service


def _execute_once(mod: ModuleType, plugin: Any, service: MagicMock) -> Any:
    # 消息体积须超触发线（context_window×0.55）：估算兜底≈字符÷2，
    # 4000 字符 ≈ 2000 tokens > 2000×0.55=1100 —— 否则 execute 早退
    # 不进压缩路径，测不到失败透传。
    ctx = mod._make_minimal_ctx(
        state={
            "context_window": 2000,
            "messages": [{"role": "user", "content": "x" * 4000, "seq": 1}],
        },
        pipeline_id="pipe-notify",
    )
    ctx.state["session_id"] = "thread-notify"
    ctx._services["context_service"] = service
    return _run(plugin.execute(ctx))


class TestCompressFailureFrontendNotify:
    def setup_method(self) -> None:
        self.mod = _load_cw_plugin()
        self.emitted: list[tuple[str, dict[str, Any], str]] = []

        async def fake_emit(event: str, payload: dict[str, Any], thread_id: str) -> None:
            self.emitted.append((event, payload, thread_id))

        self.mod.set_frontend_emit(fake_emit)

    def teardown_method(self) -> None:
        self.mod.set_frontend_emit(None)

    def test_first_failure_emits_once(self) -> None:
        plugin, service = _make_plugin_with_service(self.mod, None)
        _execute_once(self.mod, plugin, service)
        assert len(self.emitted) == 1
        event, payload, thread_id = self.emitted[0]
        assert event == "compression_failed"
        assert thread_id == "thread-notify"
        assert payload["pipeline_id"] == "pipe-notify"

    def test_consecutive_failures_emit_once(self) -> None:
        """连续失败不刷屏：同一故障周期只透传一次。"""
        plugin, service = _make_plugin_with_service(self.mod, None)
        for _ in range(3):
            _execute_once(self.mod, plugin, service)
        assert len(self.emitted) == 1

    def test_success_resets_notify_state(self) -> None:
        """失败→成功→再失败：第二次故障周期可再次透传。"""
        plugin, service = _make_plugin_with_service(self.mod, None)
        _execute_once(self.mod, plugin, service)
        assert len(self.emitted) == 1

        # 成功一轮（有效收缩：返回比原消息 token 更少的短块消息序列）
        service.compress_messages = AsyncMock(
            return_value=[{"role": "system", "content": "<compressed>ok</compressed>", "seq": 1}]
        )
        _execute_once(self.mod, plugin, service)
        assert len(self.emitted) == 1

        # 再失败 → 新故障周期，再次透传
        service.compress_messages = AsyncMock(return_value=None)
        _execute_once(self.mod, plugin, service)
        assert len(self.emitted) == 2

    def test_missing_channel_degrades_silently(self) -> None:
        """frontend.emit 未注入（None）：失败处理不炸、管线照常降级返回。"""
        self.mod.set_frontend_emit(None)
        plugin, service = _make_plugin_with_service(self.mod, None)
        result = _execute_once(self.mod, plugin, service)
        assert "messages" not in result.state_updates
