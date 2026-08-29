"""插件 SDK 结果结构契约测试（控制状态键契约 ADR 2026-08-30）。

覆盖：
  - RouteSignal 机制退役：PluginResult/OutputResult 无 route_signal 字段，
    终止/挂起/回路由一律经状态键（should_stop/ended/suspended）表达。

所有测试使用 Mock，不依赖真实服务。
"""

from __future__ import annotations

from dataclasses import fields

import pytest
from pipeline.plugin import OutputResult, PluginResult

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# RouteSignal 退役：结果结构不再携带路由信号
# ---------------------------------------------------------------------------


class TestRouteSignalRetired:
    """RouteSignal 已物理退役，结果结构不得复活该字段。"""

    @pytest.mark.unit
    def test_plugin_result_has_no_route_signal_field(self) -> None:
        """PluginResult 字段集合不含 route_signal。"""
        field_names = {f.name for f in fields(PluginResult)}
        assert "route_signal" not in field_names

    @pytest.mark.unit
    def test_output_result_has_no_route_signal_field(self) -> None:
        """OutputResult 字段集合不含 route_signal。"""
        field_names = {f.name for f in fields(OutputResult)}
        assert "route_signal" not in field_names

    @pytest.mark.unit
    def test_route_signal_not_reexported(self) -> None:
        """pipeline.types 不再导出 RouteSignal。"""
        import pipeline.types as types_mod

        assert not hasattr(types_mod, "RouteSignal")
