"""路由纯决策，引擎调度层不生成内容/不内联注入消息。

架构原则：
- 路由（route.py）：纯决策，只返回 target/signal，不写 state、不生成内容
- 引擎调度层（engine_iteration/engine_route）：可改状态字段（ENDED/CORE_TYPE/挂起），
  但禁止生成内容（format_result 写 RAW_RESULT），通知注入必须走统一入口
  consume_pending_notifications，不在各路由分支内联重复

历史问题：
  InputRouteEntry 曾有 result 字段 + format_result 方法，让路由条目能"生成内容"
  （拦截原因模板填进 RAW_RESULT）。_handle_target_end 把它写成最终输出，导致
  工具拦截变成任务最终结果、整个管道被错误终结（Bug1 根因）。
  通知注入逻辑还散落在 _handle_target_end / apply_route 三处，重复且易错。

修复：
  1. 删除 InputRouteEntry.result 字段和 format_result 方法（死代码）
  2. _handle_target_end 不再写 RAW_RESULT，只做通知检查→继续/结束
  3. 抽取 consume_pending_notifications 统一通知注入入口，三处调用替换
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ═══════════════════════════════════════════════════════════════
# P0: route.py — 路由条目不得有 result 字段和 format_result 方法
# ═══════════════════════════════════════════════════════════════


class TestRouteEntryNoContentGeneration:
    """P0: InputRouteEntry 不得携带内容生成能力（result/format_result）。

    路由只做决策，内容生成归执行点（如 tool_core 的工具失败结果）。
    """

    def test_no_result_field(self):
        """InputRouteEntry 不得有 result 字段。"""
        from pipeline.route import InputRouteEntry
        entry = InputRouteEntry(name="test", condition="True", target="end")
        assert not hasattr(entry, "result"), (
            "InputRouteEntry 不得有 result 字段——路由不生成内容"
        )

    def test_no_format_result_method(self):
        """InputRouteEntry 不得有 format_result 方法。"""
        from pipeline.route import InputRouteEntry
        entry = InputRouteEntry(name="test", condition="True", target="end")
        assert not hasattr(entry, "format_result"), (
            "InputRouteEntry 不得有 format_result 方法——路由不生成内容"
        )

    def test_no_format_result_in_source(self):
        """route.py 源码中不得再有 format_result 定义。"""
        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "pipeline" / "route.py"
        ).read_text(encoding="utf-8")
        assert "format_result" not in src, (
            "route.py 不得再有 format_result——路由不生成内容"
        )
        assert "result: str | None = None" not in src, (
            "route.py 不得再有 result 字段定义"
        )

    def test_no_re_import(self):
        """删除 format_result 后，re 模块应已移除（无消费者）。"""
        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "pipeline" / "route.py"
        ).read_text(encoding="utf-8")
        assert "import re" not in src, (
            "route.py 不应再 import re（format_result 已删除，re 无消费者）"
        )


# ═══════════════════════════════════════════════════════════════
# P0: _handle_target_end — 不生成内容，只做通知检查→继续/结束
# ═══════════════════════════════════════════════════════════════


class TestHandleTargetEndNoContentGeneration:
    """P0: _handle_target_end 不得写 RAW_RESULT（生成内容）。

    覆盖契约：end 路由后只做通知检查（consume_pending_notifications）
    和设 ENDED，不格式化任何模板写进 RAW_RESULT。
    """

    def test_no_format_result_call_in_engine_iteration(self):
        """engine_iteration.py 不得再调用 format_result。"""
        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "pipeline" / "engine_iteration.py"
        ).read_text(encoding="utf-8")
        assert "format_result" not in src, (
            "engine_iteration.py 不得再调用 format_result——_handle_target_end "
            "不生成内容"
        )

    def test_handle_target_end_not_write_raw_result(self):
        """_handle_target_end 函数体不得有写 RAW_RESULT 的赋值操作。"""
        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "pipeline" / "engine_iteration.py"
        ).read_text(encoding="utf-8")
        # 提取 _handle_target_end 函数体（从 def 到下一个顶层 def/async def）
        import re as _re
        m = _re.search(
            r"def _handle_target_end\(.*?\n(?:.*\n)*?(?=\n(?:async )?def |\Z)",
            src,
        )
        assert m, "_handle_target_end 函数必须存在"
        func_body = m.group(0)
        # 检查是否有"写 RAW_RESULT"的赋值（state[StateKeys.RAW_RESULT] = ...），
        # 而非 docstring 中提到 RAW_RESULT 这个词
        assert "RAW_RESULT] =" not in func_body, (
            "_handle_target_end 不得写 RAW_RESULT——引擎调度层不生成内容"
        )

    def test_handle_target_end_uses_consume_notifications(self):
        """_handle_target_end 必须用 consume_pending_notifications。"""
        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "pipeline" / "engine_iteration.py"
        ).read_text(encoding="utf-8")
        assert "consume_pending_notifications(engine, state)" in src, (
            "_handle_target_end 必须调用 consume_pending_notifications 统一处理通知"
        )


# ═══════════════════════════════════════════════════════════════
# P0: consume_pending_notifications — 统一通知注入入口
# ═══════════════════════════════════════════════════════════════


class TestConsumePendingNotifications:
    """P0: consume_pending_notifications 是唯一的通知注入入口。

    覆盖契约：drain_inject_queue → 过滤空白 → 非空注入 messages/user_input/CORE_TYPE。
    """

    def test_function_exists(self):
        """consume_pending_notifications 必须存在。"""
        from pipeline.engine_iteration import consume_pending_notifications
        assert callable(consume_pending_notifications)

    @pytest.mark.asyncio
    async def test_no_notifications_returns_false(self):
        """无待处理通知 → 返回 False，不改 state。"""
        from pipeline.engine_iteration import consume_pending_notifications
        engine = MagicMock()
        engine.drain_inject_queue.return_value = []
        engine.pipeline_id = "p1"
        state = {"messages": []}

        result = await consume_pending_notifications(engine, state)

        assert result is False
        assert state["messages"] == [], "无通知时不应改 messages"

    @pytest.mark.asyncio
    async def test_empty_notifications_returns_false(self):
        """通知全是空白 → 返回 False。"""
        from pipeline.engine_iteration import consume_pending_notifications
        engine = MagicMock()
        engine.drain_inject_queue.return_value = [("", "user"), ("  ", "system")]
        engine.pipeline_id = "p1"
        state = {"messages": []}

        result = await consume_pending_notifications(engine, state)
        assert result is False

    @pytest.mark.asyncio
    async def test_real_notifications_injected(self):
        """有实质通知 → 注入 messages/user_input，返回 True。

        user 注入写 user_input + messages；system 通知只写 messages。
        """
        from pipeline.engine_iteration import consume_pending_notifications
        engine = MagicMock()
        engine.drain_inject_queue.return_value = [
            ("任务完成", "user"),
            ("子任务更新", "user"),
        ]
        engine.pipeline_id = "p1"
        state = {"messages": []}

        result = await consume_pending_notifications(engine, state)

        assert result is True
        assert len(state["messages"]) == 1, "user 通知应合并为一条 user 消息"
        assert "任务完成" in state["messages"][0]["content"]
        assert "子任务更新" in state["messages"][0]["content"]
        assert state["user_input"]  # user source 写 user_input
        from pipeline.types import StateKeys
        assert state[StateKeys.CORE_TYPE] == "llm_call"


# ═══════════════════════════════════════════════════════════════
# P0: apply_route — 通知注入走统一函数，不内联重复
# ═══════════════════════════════════════════════════════════════


class TestApplyRouteUsesUnifiedNotifications:
    """P0: apply_route 的通知注入必须走 consume_pending_notifications。

    覆盖契约：engine_route.py 不得内联重复通知注入逻辑（drain_inject_queue
    + 拼 messages），必须调用统一函数。
    """

    def test_apply_route_uses_consume_notifications(self):
        """apply_route 必须调用 consume_pending_notifications 而非内联注入。"""
        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "pipeline" / "engine_route.py"
        ).read_text(encoding="utf-8")
        assert "consume_pending_notifications(engine, state)" in src, (
            "apply_route 必须调用 consume_pending_notifications 统一处理通知，"
            "不得内联重复 drain_inject_queue + 拼 messages"
        )

    def test_apply_route_no_drain_inject_queue_direct(self):
        """apply_route 不得直接调用 drain_inject_queue（应走统一函数）。"""
        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "pipeline" / "engine_route.py"
        ).read_text(encoding="utf-8")
        # drain_inject_queue 不应再在 engine_route.py 直接调用
        assert "engine.drain_inject_queue()" not in src, (
            "engine_route.py 不得直接调用 drain_inject_queue——通知注入统一走 "
            "consume_pending_notifications"
        )
