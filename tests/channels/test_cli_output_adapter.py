# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""channel_cli 输出适配器测试（状态栏状态对象化后行为契约）。

覆盖 StatusBarState 数据对象、StatusBarRenderer 渲染对状态的响应、
显示截断辅助函数 _truncate 的行为边界。
"""

from __future__ import annotations

import io

import pytest

from tests.channels.conftest import use_channel

pytestmark = pytest.mark.unit  # 0.2 TDD 分层：单元测试

use_channel("cli")
from cli_output_adapter import (  # noqa: E402
    CLIOutputAdapter,
    StatusBarRenderer,
    StatusBarState,
    _truncate,
)

# ═══════════════════════════════════════════════════════════
# StatusBarState / StatusBarRenderer
# ═══════════════════════════════════════════════════════════


class TestStatusBarState:
    """状态数据对象：默认值与独立性。"""

    def test_defaults(self) -> None:
        state = StatusBarState()
        # 抽样关键字段断默认值（剩余字段同源构造，无逐字段魔法值）
        assert state.agent_name == "Agent OS"
        assert state.mode == "normal"
        assert state.turn_count == 0
        assert state.context_pct == 0.0
        assert state.is_processing is False
        assert state.pipeline_running is False

    def test_instances_are_independent(self) -> None:
        a, b = StatusBarState(), StatusBarState()
        # 性质断言：两次构造产出等价但互不共享的状态（无可变默认共享缺陷）
        assert a == b
        b.turn_count = 9
        assert a.turn_count == 0

    def test_renderer_uses_injected_state_object(self) -> None:
        custom = StatusBarState(agent_name="灵汐", mode="auto", turn_count=5)
        renderer = StatusBarRenderer(custom)
        assert renderer.state is custom  # 注入即持有，读写同一份

        fresh_a, fresh_b = StatusBarRenderer(), StatusBarRenderer()
        assert fresh_a.state is not fresh_b.state
        assert fresh_a.state == fresh_b.state


class TestStatusBarRenderer:
    """渲染输出跟随状态变化。"""

    def test_render_reflects_left_side_state(self) -> None:
        renderer = StatusBarRenderer()
        renderer.state.agent_name = "灵汐"
        renderer.state.model_name = "deepseek/deepseek-v3"
        renderer.state.turn_count = 5
        renderer.state.context_pct = 62.0
        renderer.state.task_count = 3
        renderer.state.is_processing = True
        renderer.state.mode = "auto"

        plain = renderer.render().plain
        assert "[AUTO]" in plain
        assert "灵汐" in plain
        assert "deepseek-v3" in plain  # 模型名取末段短名
        assert "轮次 5" in plain
        assert "ctx 62%" in plain
        assert "[task]3" in plain
        assert "..." in plain  # 处理中指示

    def test_render_reflects_right_side_state(self) -> None:
        renderer = StatusBarRenderer()
        renderer.state.running_task_count = 1
        renderer.state.pending_task_count = 2
        renderer.state.completed_task_count = 4
        renderer.state.failed_task_count = 1
        renderer.state.pipeline_running = True
        renderer.state.pipeline_iteration = 7
        renderer.state.pipeline_max_iterations = 20

        plain = renderer.render().plain
        assert "run:1" in plain
        assert "pend:2" in plain
        assert "done:4" in plain
        assert "fail:1" in plain
        assert "loop 7/20" in plain

    def test_render_default_has_no_optional_segments(self) -> None:
        """全默认状态下：无轮次/任务/循环段。"""
        plain = StatusBarRenderer().render().plain
        assert "轮次" not in plain
        assert "tasks [" not in plain
        assert "loop" not in plain

    def test_render_simple_format(self) -> None:
        renderer = StatusBarRenderer()
        renderer.state.mode = "plan"
        renderer.state.agent_name = "灵汐"
        assert renderer.render_simple() == "[PLAN] 灵汐"


class TestCLIOutputAdapterStatusBar:
    """适配器与状态栏的接线（转发层删除后的直接状态面）。"""

    def test_status_bar_property_exposes_active_renderer(self) -> None:
        buf = io.StringIO()

        from rich.console import Console

        adapter = CLIOutputAdapter(console=Console(file=buf, width=120))
        adapter.status_bar.state.turn_count = 2
        adapter.render_status_bar()
        out = buf.getvalue()
        assert "轮次 2" in out

    def test_state_swap_visible_after_replacement(self) -> None:
        buf = io.StringIO()

        from rich.console import Console

        adapter = CLIOutputAdapter(console=Console(file=buf, width=120))
        adapter.status_bar.state = StatusBarState(agent_name="替换后", mode="plan")
        adapter.render_status_bar()
        assert "替换后" in buf.getvalue()


# ═══════════════════════════════════════════════════════════
# 工具调用展示（截断辅助经生产路径消费）
# ═══════════════════════════════════════════════════════════


class TestShowToolCall:
    """show_tool_call 参数显示：截断/私有键过滤/超三参省略。"""

    @staticmethod
    def _adapter() -> tuple[CLIOutputAdapter, io.StringIO]:
        buf = io.StringIO()

        from rich.console import Console

        adapter = CLIOutputAdapter(console=Console(file=buf, width=200))
        return adapter, buf

    def test_args_rendered_and_private_keys_filtered(self) -> None:
        adapter, buf = self._adapter()
        adapter.show_tool_call("read_file", {"path": "/tmp/a.txt", "_secret": "x"})
        out = buf.getvalue()
        assert "read_file(" in out
        assert "path=/tmp/a.txt" in out
        assert "_secret" not in out  # 下划线开头参数不外显

    def test_more_than_three_args_collapsed_with_ellipsis(self) -> None:
        adapter, buf = self._adapter()
        adapter.show_tool_call("multi", {"a": 1, "b": 2, "c": 3, "d": 4})
        out = buf.getvalue()
        # 性质断言：只显前三项，剩余以 ", ..." 汇总
        for key in ("a=1", "b=2", "c=3"):
            assert key in out
        assert "d=4" not in out
        assert "..." in out

    def test_pending_confirmation_suffix(self) -> None:
        adapter, buf = self._adapter()
        adapter.show_tool_call("write_file", {"p": 1}, pending=True)
        assert "等待确认" in buf.getvalue()


# ═══════════════════════════════════════════════════════════
# _truncate 显示截断
# ═══════════════════════════════════════════════════════════


class TestTruncate:
    def test_short_values_pass_through(self) -> None:
        assert _truncate("hi") == "hi"
        assert _truncate("") == ""
        assert _truncate(12345) == "12345"  # 非字符串先 str 化

    def test_long_values_truncated_with_ellipsis(self) -> None:
        result = _truncate("x" * 40)  # 默认 max_len=30
        assert len(result) == 33  # 30 + "..."
        assert result.endswith("...")
        assert result.startswith("x" * 30)

    def test_boundary_exact_length_not_truncated(self) -> None:
        text = "y" * 50
        # 恰等于上限时不加省略号（len > max_len 才截）
        assert _truncate(text, 50) == text
        assert _truncate(text, 49).endswith("...")

    @pytest.mark.parametrize(
        ("value", "max_len"),
        [("a" * 99, 50), (12345678, 4), (["long", "list"], 8)],
    )
    def test_output_bounded_and_marks_cut(self, value: object, max_len: int) -> None:
        result = _truncate(value, max_len)
        # 性质断言：任意输入的输出长度有界且超限时以 ... 标记截断
        assert len(result) <= max_len + 3
        if len(str(value)) > max_len:
            assert result.endswith("...")
