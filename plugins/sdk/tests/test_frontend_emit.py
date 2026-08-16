# @feature: FP-0.2.可观测性 | @vision: V3 可嵌入 | @ci: python-test
"""frontend.emit capability 测试（task_observability 任务 1/2 共享前置）。

ADR §3.5：frontend.emit 是「插件 → 内核 → 前端」的一次性事件推送出口。
本测试钉死 SDK 侧契约：
- "frontend" 进入 STANDARD_CAPABILITIES 清单（文档/校验参考）
- FrontendEmitter 包装 CapabilityHandle，emit(event, payload) 经
  notify("emit", {"event": ..., "payload": ...}) fire-and-forget 发往内核
- 内核未声明 frontend capability 时（旧内核）from_plugin 返回不可用实例，
  emit 静默跳过——可观测性推送失败绝不阻断插件主流程
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agentos_plugin_sdk import AgentOSPlugin, CapabilityHandle
from agentos_plugin_sdk.capability import STANDARD_CAPABILITIES, FrontendEmitter


class TestFrontendCapabilityNamespace:
    """frontend 命名空间注册。"""

    def test_standard_capabilities_contains_frontend(self) -> None:
        assert "frontend" in STANDARD_CAPABILITIES

    def test_frontend_handle_injected_when_declared(self) -> None:
        """内核声明 frontend capability 时，握手后 get_capability 可用。"""
        plugin = AgentOSPlugin("track_pipeline")
        plugin._on_initialize({"capabilities": {"frontend": {}}, "config": {}})
        cap = plugin.get_capability("frontend")
        assert cap.name == "frontend"


class TestFrontendEmitter:
    """FrontendEmitter 推送语义。"""

    def _make_emitter(self) -> tuple[FrontendEmitter, AsyncMock]:
        notify_fn = AsyncMock()
        handle = CapabilityHandle("frontend", notify_fn=notify_fn)
        return FrontendEmitter(handle), notify_fn

    @pytest.mark.asyncio
    async def test_emit_sends_event_and_payload(self) -> None:
        emitter, notify_fn = self._make_emitter()
        await emitter.emit("cost_update", {"pipeline_id": "p1", "total_tokens": 42})
        notify_fn.assert_awaited_once_with(
            "emit",
            {"event": "cost_update", "payload": {"pipeline_id": "p1", "total_tokens": 42}},
        )

    @pytest.mark.asyncio
    async def test_emit_is_fire_and_forget(self) -> None:
        """通道异常不外泄——推送失败绝不阻断插件主流程。"""
        notify_fn = AsyncMock(side_effect=RuntimeError("channel closed"))
        handle = CapabilityHandle("frontend", notify_fn=notify_fn)
        emitter = FrontendEmitter(handle)
        # 不抛异常
        await emitter.emit("cost_update", {"total_tokens": 1})

    def test_unavailable_emitter_swallows_emit(self) -> None:
        emitter = FrontendEmitter(None)
        assert emitter.available is False

    @pytest.mark.asyncio
    async def test_unavailable_emitter_emit_noop(self) -> None:
        emitter = FrontendEmitter(None)
        await emitter.emit("cost_update", {"total_tokens": 1})  # 静默跳过，不抛

    def test_from_plugin_resolves_declared_capability(self) -> None:
        plugin = AgentOSPlugin("track_pipeline")
        notify_fn = AsyncMock()
        plugin._on_initialize({"capabilities": {"frontend": {}}, "config": {}})
        plugin._capabilities["frontend"]._notify_fn = notify_fn
        emitter = FrontendEmitter.from_plugin(plugin)
        assert emitter is not None and emitter.available is True

    def test_from_plugin_returns_none_when_undeclared(self) -> None:
        """旧内核未声明 frontend → from_plugin 优雅降级。"""
        plugin = AgentOSPlugin("track_pipeline")
        plugin._on_initialize({"capabilities": {}, "config": {}})
        emitter = FrontendEmitter.from_plugin(plugin)
        assert emitter is None
