# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""StuckDetector 单元测试——卡死检测的纯逻辑路径。

覆盖：滑动窗口维护、工具调用重复、输出完全重复、输出高度相似、
快照提取、相似度计算、配置覆盖、未达阈值不报。
"""

from __future__ import annotations

from typing import Any

import pytest
from pipeline.plugin import PluginContext
from pipeline.types import StateKeys

pytestmark = pytest.mark.unit


# ============================================================
# 辅助
# ============================================================


def _ctx(state: dict[str, Any] | None = None) -> PluginContext:
    return PluginContext(state=state or {}, config={})


def _state_with_tool(name: str, args: dict[str, Any], result: Any) -> dict[str, Any]:
    """构造一轮带工具调用与结果的状态（llm_call 轮，arguments 为生产方真实键名）。"""
    return {
        StateKeys.RAW_TOOL_CALLS: [{"name": name, "arguments": args}],
        StateKeys.RAW_RESULT: result,
        StateKeys.ITERATION: 1,
    }


# ============================================================
# 配置与基本属性
# ============================================================


class TestConfig:
    def test_默认配置(self) -> None:
        from plugin import StuckDetector

        d = StuckDetector()
        assert d.name == "stuck_detector"
        assert d.priority == 15

    @pytest.mark.asyncio
    async def test_默认repeat_threshold经公共行为生效(self) -> None:
        """默认阈值 3 经 execute() 可观察：连 2 轮重复不报，第 3 轮报。

        （P9：原断言 d._window_size/_similarity_threshold/_repeat_threshold
        私有字段，改为经公共行为验证阈值生效；window_size/similarity_threshold
        的行为面由 TestExecute 滑动窗口与 TestOutputRepeat 相似度用例覆盖。）
        """
        from plugin import StuckDetector

        d = StuckDetector()
        state = _state_with_tool("file_write", {"p": "/x"}, "same")
        res1 = await d.execute(_ctx(state))
        state.update(res1.state_updates)  # 引擎口径：updates 并回 state
        res2 = await d.execute(_ctx(state))
        state.update(res2.state_updates)
        res3 = await d.execute(_ctx(state))
        assert res1.state_updates["stuck_detected"] is False
        assert res2.state_updates["stuck_detected"] is False
        assert res3.state_updates["stuck_detected"] is True

    @pytest.mark.asyncio
    async def test_自定义配置覆盖默认(self) -> None:
        """repeat_threshold=2 经公共行为生效：第 2 轮重复即报卡死。"""
        from plugin import StuckDetector

        d = StuckDetector(config={"repeat_threshold": 2, "priority": 99})
        assert d.priority == 99
        state = _state_with_tool("t", {}, "same")
        res1 = await d.execute(_ctx(state))
        state.update(res1.state_updates)  # 引擎口径：updates 并回 state
        res2 = await d.execute(_ctx(state))
        assert res1.state_updates["stuck_detected"] is False
        assert res2.state_updates["stuck_detected"] is True


# ============================================================
# _compute_similarity
# ============================================================


class TestSimilarity:
    def test_相同文本相似度为1(self) -> None:
        from plugin import StuckDetector

        d = StuckDetector()
        assert d._compute_similarity("abc", "abc") == 1.0

    def test_空文本相似度为0(self) -> None:
        from plugin import StuckDetector

        d = StuckDetector()
        assert d._compute_similarity("", "abc") == 0.0
        assert d._compute_similarity("abc", "") == 0.0

    def test_差异文本相似度小于1(self) -> None:
        from plugin import StuckDetector

        d = StuckDetector()
        sim = d._compute_similarity("hello world", "goodbye universe")
        assert 0.0 <= sim < 1.0


# ============================================================
# _take_snapshot
# ============================================================


class TestSnapshot:
    def test_提取工具签名与结果文本(self) -> None:
        from plugin import StuckDetector

        d = StuckDetector()
        snap = d._take_snapshot(_state_with_tool("file_write", {"path": "/a", "content": "x"}, "done"))
        assert "file_write(" in snap["tool_signature"]
        assert snap["result_text"] == "done"
        assert snap["iteration"] == 1

    def test_结果文本截断到500字符(self) -> None:
        from plugin import StuckDetector

        d = StuckDetector()
        long_text = "x" * 1000
        snap = d._take_snapshot({StateKeys.RAW_RESULT: long_text})
        assert len(snap["result_text"]) == 500

    def test_无工具调用时签名为空(self) -> None:
        from plugin import StuckDetector

        d = StuckDetector()
        snap = d._take_snapshot({StateKeys.RAW_RESULT: "r"})
        assert snap["tool_signature"] == ""

    def test_结果为None时文本为空(self) -> None:
        from plugin import StuckDetector

        d = StuckDetector()
        snap = d._take_snapshot({StateKeys.RAW_RESULT: None})
        assert snap["result_text"] == ""


# ============================================================
# _check_tool_repeat
# ============================================================


class TestToolRepeat:
    def test_连续N次相同工具调用报卡死(self) -> None:
        from plugin import StuckDetector

        d = StuckDetector(config={"repeat_threshold": 3})
        history = [
            {"tool_signature": "file_write(/a)"},
            {"tool_signature": "file_write(/a)"},
            {"tool_signature": "file_write(/a)"},
        ]
        reason = d._check_tool_repeat(history)
        assert "Tool call repeated 3 times" in reason
        assert "file_write(/a)" in reason

    def test_签名不同不报(self) -> None:
        from plugin import StuckDetector

        d = StuckDetector(config={"repeat_threshold": 3})
        history = [
            {"tool_signature": "a"},
            {"tool_signature": "b"},
            {"tool_signature": "a"},
        ]
        assert d._check_tool_repeat(history) == ""

    def test_历史不足阈值不报(self) -> None:
        from plugin import StuckDetector

        d = StuckDetector(config={"repeat_threshold": 3})
        assert d._check_tool_repeat([{"tool_signature": "a"}]) == ""

    def test_签名为空不报(self) -> None:
        from plugin import StuckDetector

        d = StuckDetector(config={"repeat_threshold": 3})
        history = [{"tool_signature": ""}, {"tool_signature": ""}, {"tool_signature": ""}]
        assert d._check_tool_repeat(history) == ""


# ============================================================
# _check_output_repeat
# ============================================================


class TestOutputRepeat:
    def test_连续N次完全相同输出报卡死(self) -> None:
        from plugin import StuckDetector

        d = StuckDetector(config={"repeat_threshold": 3})
        history = [
            {"result_text": "same"},
            {"result_text": "same"},
            {"result_text": "same"},
        ]
        reason = d._check_output_repeat(history)
        assert "identically" in reason

    def test_高度相似输出报卡死(self) -> None:
        from plugin import StuckDetector

        d = StuckDetector(config={"repeat_threshold": 3, "similarity_threshold": 0.7})
        # 仅末尾差异，相似度高
        history = [
            {"result_text": "the quick brown fox jumps"},
            {"result_text": "the quick brown fox jumps over"},
            {"result_text": "the quick brown fox jumps again"},
        ]
        reason = d._check_output_repeat(history)
        assert "similarity" in reason

    def test_输出差异大不报(self) -> None:
        from plugin import StuckDetector

        d = StuckDetector(config={"repeat_threshold": 3, "similarity_threshold": 0.9})
        history = [
            {"result_text": "alpha"},
            {"result_text": "completely different content here"},
            {"result_text": "yet another distinct value"},
        ]
        assert d._check_output_repeat(history) == ""

    def test_输出为空不报(self) -> None:
        from plugin import StuckDetector

        d = StuckDetector(config={"repeat_threshold": 3})
        history = [{"result_text": ""}, {"result_text": ""}, {"result_text": ""}]
        assert d._check_output_repeat(history) == ""


# ============================================================
# execute 端到端（含滑动窗口）
# ============================================================


class TestExecute:
    @pytest.mark.asyncio
    async def test_正常无卡死返回False(self) -> None:
        from plugin import StuckDetector

        d = StuckDetector()
        result = await d.execute(_ctx({StateKeys.RAW_RESULT: "ok", StateKeys.ITERATION: 1}))
        assert result.state_updates["stuck_detected"] is False
        assert result.state_updates["stuck_reason"] == ""

    @pytest.mark.asyncio
    async def test_连续三轮相同工具与结果触发卡死(self) -> None:
        from plugin import StuckDetector

        d = StuckDetector(config={"repeat_threshold": 3})
        state = _state_with_tool("file_write", {"p": "/x"}, "same")
        # 连续执行三次同样的状态（引擎口径：每轮 updates 并回）
        for _ in range(3):
            res = await d.execute(_ctx(state))
            state.update(res.state_updates)
        # 第三次应报告卡死，原因同时含工具重复与输出重复
        assert res.state_updates["stuck_detected"] is True
        reason = res.state_updates["stuck_reason"]
        assert "Tool call repeated" in reason
        assert "Output repeated" in reason

    @pytest.mark.asyncio
    async def test_滑动窗口不超过window_size(self) -> None:
        from plugin import StuckDetector

        d = StuckDetector(config={"window_size": 3})
        state: dict[str, Any] = {}
        for i in range(7):
            state[StateKeys.RAW_RESULT] = f"r{i}"
            state[StateKeys.ITERATION] = i
            res = await d.execute(_ctx(state))
            state.update(res.state_updates)  # 引擎口径：updates 并回 state
        history = state["stuck_history"]
        assert len(history) == 3
        # 保留的是最后 3 个
        assert history[0]["result_text"] == "r4"
        assert history[-1]["result_text"] == "r6"

    @pytest.mark.asyncio
    async def test_未达repeat_threshold即使重复也不报(self) -> None:
        from plugin import StuckDetector

        d = StuckDetector(config={"repeat_threshold": 3})
        state = _state_with_tool("t", {}, "same")
        res1 = await d.execute(_ctx(state))
        state.update(res1.state_updates)  # 引擎口径：updates 并回 state
        res2 = await d.execute(_ctx(state))
        assert res1.state_updates["stuck_detected"] is False
        assert res2.state_updates["stuck_detected"] is False


# ============================================================
# 真实链路口径（08-30 修复回归）
# ============================================================


class TestRealChainContract:
    @pytest.mark.asyncio
    async def test_工具轮跳过检测零更新(self) -> None:
        """core_type=tool_execute 轮 raw_tool_calls 已清/文本是残留 → 跳过不产出快照。"""
        from plugin import StuckDetector

        d = StuckDetector()
        state = {
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [],
            StateKeys.RAW_RESULT: "stale",
        }
        res = await d.execute(_ctx(state))
        assert res.state_updates == {}

    @pytest.mark.asyncio
    async def test_历史经state回传实例间不串扰(self) -> None:
        """stuck_history 经 state_updates 回传（per-run 隔离）——sidecar 单例跨任务不共享。"""
        from plugin import StuckDetector

        d1, d2 = StuckDetector(), StuckDetector()
        state = _state_with_tool("t", {"p": 1}, "same")
        await d1.execute(_ctx(state))
        res2 = await d2.execute(_ctx(_state_with_tool("t", {"p": 1}, "other")))
        # d2 从空 state 起步，不受 d1 实例内部状态影响
        assert res2.state_updates["stuck_detected"] is False
        assert len(res2.state_updates["stuck_history"]) == 1

    @pytest.mark.asyncio
    async def test_参数str_json归一参与签名(self) -> None:
        """arguments 为 JSON 字符串（LLM 返回原形）与等价 dict 同签名。"""
        from plugin import StuckDetector

        d = StuckDetector(config={"repeat_threshold": 3})
        states = [
            {
                StateKeys.RAW_TOOL_CALLS: [{"name": "t", "arguments": '{"p": 1}'}],
                StateKeys.RAW_RESULT: "same",
            },
            {
                StateKeys.RAW_TOOL_CALLS: [{"name": "t", "arguments": {"p": 1}}],
                StateKeys.RAW_RESULT: "same",
            },
            {
                StateKeys.RAW_TOOL_CALLS: [{"name": "t", "arguments": '{"p": 1}'}],
                StateKeys.RAW_RESULT: "same",
            },
        ]
        state = states[0]
        last = None
        for i in range(len(states)):
            if i > 0:
                state.update(
                    {
                        StateKeys.RAW_TOOL_CALLS: states[i][StateKeys.RAW_TOOL_CALLS],
                        StateKeys.RAW_RESULT: states[i][StateKeys.RAW_RESULT],
                    }
                )
            last = await d.execute(_ctx(state))
            state.update(last.state_updates)
        assert "Tool call repeated 3 times" in last.state_updates["stuck_reason"]

    @pytest.mark.asyncio
    async def test_纯文本轮不稀释工具重复连续性(self) -> None:
        """零信号轮（无调用无文本）不入窗：签名与纯文本轮交替仍可判工具重复。"""
        from plugin import StuckDetector

        d = StuckDetector(config={"repeat_threshold": 3})
        tool_state = _state_with_tool("t", {"p": 1}, None)
        text_state = {StateKeys.RAW_RESULT: "thinking..."}
        state = dict(tool_state)
        for i in range(5):
            st = tool_state if i % 2 == 0 else text_state
            state.update(
                {
                    StateKeys.RAW_TOOL_CALLS: st.get(StateKeys.RAW_TOOL_CALLS, []),
                    StateKeys.RAW_RESULT: st.get(StateKeys.RAW_RESULT),
                }
            )
            res = await d.execute(_ctx(state))
            state.update(res.state_updates)
        assert res.state_updates["stuck_detected"] is True
        assert "Tool call repeated 3 times" in res.state_updates["stuck_reason"]
