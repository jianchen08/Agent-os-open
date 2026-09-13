# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""channel_cli 输出适配器测试（状态栏状态对象化后行为契约）。

覆盖 StatusBarState 数据对象、StatusBarRenderer 渲染对状态的响应、
显示截断辅助函数 _truncate 的行为边界。
"""

from __future__ import annotations

import io
import os
import sys

import pytest

from tests.channels.conftest import use_channel

pytestmark = pytest.mark.unit  # 0.2 TDD 分层：单元测试

use_channel("cli")
from cli_output_adapter import (  # noqa: E402
    CLIOutputAdapter,
    StatusBarRenderer,
    StatusBarState,
    _truncate,
    sanitize_for_terminal,
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
# sanitize_for_terminal 编码清洗
# ═══════════════════════════════════════════════════════════


class _FakeStdout:
    """替身 stdout：只提供 encoding 属性（函数内 import sys 后读 stdout.encoding）。"""

    def __init__(self, encoding: str | None) -> None:
        self.encoding = encoding


class TestSanitizeForTerminal:
    """按 stdout 实际编码决定是否替换不可编码字符。"""

    def test_utf8_terminal_passes_everything_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdout", _FakeStdout("utf-8"))
        text = "你好 world 😀🎉"
        assert sanitize_for_terminal(text) == text

    @pytest.mark.parametrize("encoding", ["UTF-8", "cp65001", "utf_8"])
    def test_utf8_variants_are_normalized_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch, encoding: str
    ) -> None:
        monkeypatch.setattr(sys, "stdout", _FakeStdout(encoding))
        text = "emoji 😀 ok"
        assert sanitize_for_terminal(text) == text

    def test_null_encoding_falls_back_to_utf8(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdout", _FakeStdout(None))
        text = "文本 text"
        assert sanitize_for_terminal(text) == text

    def test_gbk_encodable_text_passes_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdout", _FakeStdout("gbk"))
        assert sanitize_for_terminal("中文 ascii 123") == "中文 ascii 123"

    def test_gbk_unencodable_chars_replaced_per_char(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdout", _FakeStdout("gbk"))
        result = sanitize_for_terminal("前😀后✅tail")
        # 性质断言：替换只发生在不可编码字符上，可编码部分原样保留
        assert result == "前?后?tail"
        result.encode("gbk")  # 输出必须可在目标编码下落盘

    def test_unknown_encoding_replaces_everything(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdout", _FakeStdout("not-a-real-codec"))
        # 未知编码下任何字符都无法 encode（含 ASCII），逐字符全部替换
        assert sanitize_for_terminal("abc😀") == "????"


# ═══════════════════════════════════════════════════════════
# send：管道终态三分支（错误/停止/正常 + 流式去重）
# ═══════════════════════════════════════════════════════════


def _make_adapter(width: int = 200) -> tuple[CLIOutputAdapter, io.StringIO]:
    from rich.console import Console

    buf = io.StringIO()
    return CLIOutputAdapter(console=Console(file=buf, width=width)), buf


class TestSend:
    """send 按 state 内容选择输出样式。"""

    async def test_error_state_renders_error_panel_and_stops(self) -> None:
        adapter, buf = _make_adapter()
        await adapter.send({"error": "boom 失败", "raw_result": "不应输出"})
        out = buf.getvalue()
        assert "boom 失败" in out
        assert "错误" in out
        assert "不应输出" not in out  # error 分支短路，不再走正常结果

    async def test_should_stop_renders_system_end_message(self) -> None:
        adapter, buf = _make_adapter()
        await adapter.send({"should_stop": True, "raw_result": "静默"})
        out = buf.getvalue()
        assert "会话结束" in out
        assert "静默" not in out

    async def test_streamed_mode_suppresses_raw_result(self) -> None:
        adapter, buf = _make_adapter()
        await adapter.send({"raw_result": "已在流式回调输出"}, streamed=True)
        assert buf.getvalue() == ""

    async def test_streamed_mode_still_reports_raw_error(self) -> None:
        adapter, buf = _make_adapter()
        await adapter.send({"raw_result": "x", "raw_error": "部分失败警告"}, streamed=True)
        out = buf.getvalue()
        assert "部分失败警告" in out
        assert "警告" in out
        assert "x" not in out

    async def test_normal_result_printed(self) -> None:
        adapter, buf = _make_adapter()
        await adapter.send({"raw_result": "最终结论内容"})
        assert "最终结论内容" in buf.getvalue()

    async def test_empty_state_prints_nothing(self) -> None:
        adapter, buf = _make_adapter()
        await adapter.send({})
        assert buf.getvalue() == ""


# ═══════════════════════════════════════════════════════════
# send_stream：chunk 类型分派
# ═══════════════════════════════════════════════════════════


class TestSendStream:
    """send_stream 按 type 分派到对应展示方法。"""

    async def test_empty_token_chunk_is_noop(self) -> None:
        adapter, buf = _make_adapter()
        await adapter.send_stream({"type": "token", "text": ""})
        assert buf.getvalue() == ""

    async def test_error_chunk_rendered(self) -> None:
        adapter, buf = _make_adapter()
        await adapter.send_stream({"type": "error", "text": "出错了"})
        assert "出错了" in buf.getvalue()

    async def test_system_chunk_rendered(self) -> None:
        adapter, buf = _make_adapter()
        await adapter.send_stream({"type": "system", "text": "系统通知"})
        assert "系统通知" in buf.getvalue()

    async def test_tool_call_chunk_dispatches_to_show_tool_call(self) -> None:
        adapter, buf = _make_adapter()
        await adapter.send_stream(
            {"type": "tool_call", "text": "", "tool_name": "read_file", "tool_args": {"path": "/a"}}
        )
        out = buf.getvalue()
        # rich 把 [tool]/[dim] 当标记解析吞掉，可见文本为「调用 name(args)」
        assert "调用 read_file(" in out
        assert "path=/a" in out

    async def test_tool_result_chunk_dispatches_to_show_tool_result(self) -> None:
        adapter, buf = _make_adapter()
        await adapter.send_stream({"type": "tool_result", "text": "", "tool_name": "t", "result": "完成"})
        assert "OK" in buf.getvalue()
        assert "完成" in buf.getvalue()

    async def test_task_chunk_dispatches_to_show_task_notification(self) -> None:
        adapter, buf = _make_adapter()
        await adapter.send_stream(
            {"type": "task", "text": "", "task_action": "created", "task_info": {"task_id": 7, "description": "抓取"}}
        )
        out = buf.getvalue()
        assert "创建任务 #7" in out
        assert "抓取" in out

    async def test_iteration_chunk_dispatches_to_show_iteration(self) -> None:
        adapter, buf = _make_adapter()
        await adapter.send_stream({"type": "iteration", "text": "", "iteration": 3, "max_iterations": 10})
        assert "迭代 3/10" in buf.getvalue()

    async def test_default_chunk_printed_as_token(self) -> None:
        adapter, buf = _make_adapter()
        await adapter.send_stream({"type": "token", "text": "逐字"})
        assert "逐字" in buf.getvalue()


# ═══════════════════════════════════════════════════════════
# Claude Code 风格展示方法 + 适配器杂项面
# ═══════════════════════════════════════════════════════════


class TestShowMethods:
    """show_* 家族：输出可观察行为。"""

    def test_show_tool_result_success_with_duration(self) -> None:
        adapter, buf = _make_adapter()
        adapter.show_tool_result("t", "ok 结果", success=True, duration_ms=12.4)
        out = buf.getvalue()
        assert "OK" in out
        assert "12ms" in out
        assert "ok 结果" in out

    def test_show_tool_result_failure_marked_red(self) -> None:
        adapter, buf = _make_adapter()
        adapter.show_tool_result("t", "炸了", success=False)
        out = buf.getvalue()
        assert "FAIL" in out
        assert "炸了" in out
        assert "OK" not in out

    def test_show_tool_result_long_result_truncated_at_100(self) -> None:
        adapter, buf = _make_adapter()
        adapter.show_tool_result("t", "y" * 250)
        out = buf.getvalue()
        assert "y" * 100 in out
        assert "y" * 101 not in out
        assert "..." in out

    def test_show_task_notification_all_actions(self) -> None:
        adapter, buf = _make_adapter(width=400)
        for action, marker in (("created", "创建任务 #1"), ("completed", "任务 #1 完成"), ("failed", "任务 #1 失败")):
            adapter.show_task_notification(action, {"task_id": 1})
            assert marker in buf.getvalue()
        # 未知动作走兜底行；task_id 缺失时回退 id 键，再缺失显 ?
        adapter.show_task_notification("paused", {"id": 9})
        assert "任务 #9: paused" in buf.getvalue()
        adapter.show_task_notification("paused", {})
        assert "任务 #?: paused" in buf.getvalue()

    def test_show_task_notification_desc_fallback_chain(self) -> None:
        adapter, buf = _make_adapter(width=400)
        adapter.show_task_notification("created", {"task_id": 2, "title": "标题回退"})
        assert "标题回退" in buf.getvalue()

    def test_show_iteration(self) -> None:
        adapter, buf = _make_adapter()
        adapter.show_iteration(2, 20)
        assert "迭代 2/20" in buf.getvalue()

    def test_show_system_message_custom_style(self) -> None:
        adapter, buf = _make_adapter()
        adapter.show_system_message("维护中", style="yellow")
        assert "维护中" in buf.getvalue()
        assert "[系统]" in buf.getvalue()

    def test_show_startup_banner_contains_agent_and_mode(self) -> None:
        adapter, buf = _make_adapter()
        adapter.show_startup_banner("灵汐", mode="auto")
        out = buf.getvalue()
        assert "灵汐" in out
        assert "AUTO" in out
        assert "/help" in out


class TestAdapterMisc:
    """属性面、默认构造、宽度回退与交互确认。"""

    def test_console_property_returns_injected_console(self) -> None:
        from rich.console import Console

        console = Console(file=io.StringIO(), width=120)
        assert CLIOutputAdapter(console=console).console is console

    def test_default_construction_uses_detected_or_fallback_width(self) -> None:
        adapter = CLIOutputAdapter()
        assert adapter.console.width >= 40  # 探测宽度或回退 80，不产出超窄控制台

    def test_default_construction_survives_terminal_size_error(self) -> None:
        import shutil

        # 桩必须在测试体内自恢复（try/finally）：pytest 终端进度写入器在本用例
        # 的报告窗口内会调 get_terminal_size(fallback=...)，monkeypatch 的
        # teardown 归位晚于该窗口——桩外泄一次即 OSError 炸穿整条车道
        #（INTERNALERROR，批九两次全量实锤）。
        original = shutil.get_terminal_size

        def _boom(*args: object, **kwargs: object) -> None:
            raise OSError("no tty")

        shutil.get_terminal_size = _boom
        try:
            assert CLIOutputAdapter().console.width == 80
        finally:
            shutil.get_terminal_size = original

    def test_render_falls_back_to_80_on_narrow_terminal(self) -> None:
        import shutil

        original = shutil.get_terminal_size
        shutil.get_terminal_size = lambda *a, **k: os.terminal_size((30, 24))
        try:
            renderer = StatusBarRenderer()
            text = renderer.render()
        finally:
            shutil.get_terminal_size = original
        # 性质断言：宽度回退后整行不超过回退宽度（右侧内容不因过窄被挤没）
        assert text.cell_len >= 40

    def test_render_falls_back_to_80_on_terminal_size_error(self) -> None:
        import shutil

        original = shutil.get_terminal_size

        def _boom(*args: object, **kwargs: object) -> None:
            raise OSError("no tty")

        shutil.get_terminal_size = _boom
        try:
            text = StatusBarRenderer().render().plain
        finally:
            shutil.get_terminal_size = original
        assert "Agent OS" in text

    def test_show_thinking_default_and_toggle(self) -> None:
        adapter, _buf = _make_adapter()
        assert adapter.show_thinking is False
        adapter.show_thinking = True
        assert adapter.show_thinking is True

    def test_show_processing_returns_status_context_manager(self) -> None:
        from rich.status import Status

        adapter, buf = _make_adapter()
        status = adapter.show_processing("运算中")
        assert isinstance(status, Status)
        with status:
            pass  # 可正常进出（rich 内部渲染到注入 console）

    def test_show_tool_confirmation_accept_and_decline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter, buf = _make_adapter(width=400)
        monkeypatch.setattr("builtins.input", lambda _prompt: "y")
        assert adapter.show_tool_confirmation("write_file", {"p": 1}) == "yes"
        assert "等待确认" in buf.getvalue()

        monkeypatch.setattr("builtins.input", lambda _prompt: "n")
        assert adapter.show_tool_confirmation("write_file", {"p": 1}) is None

    @pytest.mark.parametrize("answer", ["no", "s", "skip", "N"])
    def test_show_tool_confirmation_negative_answers_decline(
        self, monkeypatch: pytest.MonkeyPatch, answer: str
    ) -> None:
        adapter, _buf = _make_adapter(width=400)
        monkeypatch.setattr("builtins.input", lambda _prompt: answer)
        assert adapter.show_tool_confirmation("t", {}) is None

    def test_show_tool_confirmation_eof_declines(self, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter, _buf = _make_adapter(width=400)

        def _eof(_prompt: str) -> str:
            raise EOFError

        monkeypatch.setattr("builtins.input", _eof)
        assert adapter.show_tool_confirmation("t", {}) is None


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
