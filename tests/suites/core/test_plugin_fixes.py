"""plugin.py 与 types.py 修复验证单元测试。

覆盖：
  - M5: OutputResult 不重复声明 route_signal
  - P3: RouteSignal docstring 包含 decision 路由类型

所有测试使用 Mock，不依赖真实服务。
"""

from __future__ import annotations

from dataclasses import fields

import pytest
from pipeline.plugin import OutputResult, PluginResult
from pipeline.types import RouteSignal

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# M5: OutputResult 不重复声明 route_signal
# ---------------------------------------------------------------------------


class TestOutputResultNoDuplicateRouteSignal:
    """M5: OutputResult 不重复声明 route_signal 字段。"""

    @pytest.mark.unit
    def test_output_result_no_own_route_signal_annotation(self) -> None:
        """M5: OutputResult 自身注解不包含 route_signal。

        route_signal 应只在父类 PluginResult 中声明，
        OutputResult 不应重复声明。
        """
        own_annotations = getattr(OutputResult, "__annotations__", {})
        assert "route_signal" not in own_annotations

    @pytest.mark.unit
    def test_output_result_inherits_route_signal(self) -> None:
        """M5: OutputResult 通过继承拥有 route_signal 字段。

        虽然 OutputResult 自身不声明 route_signal，
        但通过继承 PluginResult 仍然可以使用该字段。
        """
        # PluginResult 应有 route_signal 注解
        parent_annotations = getattr(PluginResult, "__annotations__", {})
        assert "route_signal" in parent_annotations

        # OutputResult 的 dataclass fields 应包含 route_signal（继承）
        field_names = {f.name for f in fields(OutputResult)}
        assert "route_signal" in field_names

    @pytest.mark.unit
    def test_output_result_own_fields_empty(self) -> None:
        """M5: OutputResult 没有新增的自身字段。

        OutputResult 应仅继承 PluginResult 的字段，
        不添加任何新字段。
        """
        parent_field_names = {f.name for f in fields(PluginResult)}
        child_field_names = {f.name for f in fields(OutputResult)}
        # 子类字段集合应等于父类字段集合（无新增）
        assert child_field_names == parent_field_names

    @pytest.mark.unit
    def test_output_result_can_set_route_signal(self) -> None:
        """M5: OutputResult 实例可以正常设置 route_signal。"""
        signal = RouteSignal(route_type="end", reason="done")
        result = OutputResult(route_signal=signal)
        assert result.route_signal is signal
        assert result.route_signal.route_type == "end"

    @pytest.mark.unit
    def test_output_result_route_signal_defaults_none(self) -> None:
        """M5: OutputResult 的 route_signal 默认值为 None。"""
        result = OutputResult()
        assert result.route_signal is None


# ---------------------------------------------------------------------------
# P3: RouteSignal docstring 包含 decision 类型
# ---------------------------------------------------------------------------


class TestRouteSignalDocstring:
    """P3: RouteSignal docstring 包含 decision 路由类型。"""

    @pytest.mark.unit
    def test_route_signal_docstring_contains_decision(self) -> None:
        """P3: RouteSignal 类 docstring 应包含 decision 路由类型。"""
        assert RouteSignal.__doc__ is not None
        assert "decision" in RouteSignal.__doc__

    @pytest.mark.unit
    def test_route_signal_docstring_contains_all_types(self) -> None:
        """P3: RouteSignal docstring 应列出所有路由类型。"""
        doc = RouteSignal.__doc__
        assert doc is not None
        for expected_type in ("next_llm", "next_tool", "end", "delegate", "wait", "decision"):
            assert expected_type in doc, (
                f"RouteSignal docstring 缺少路由类型 '{expected_type}'"
            )

    @pytest.mark.unit
    def test_route_signal_route_type_accepts_decision(self) -> None:
        """P3: RouteSignal 实例的 route_type 字段应接受 decision 值。"""
        signal = RouteSignal(route_type="decision", reason="needs user input")
        assert signal.route_type == "decision"
        assert signal.reason == "needs user input"
