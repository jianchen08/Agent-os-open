"""卡死检测 Output 插件。

检测管道循环中的卡死状态，包括工具调用重复、输出内容重复
和无进展检测。检测到卡死时标记 state 并发布 stuck 事件。

检测策略：
1. 相同工具调用重复（连续 N 次相同 tool_name + args）
2. 相同输出重复（连续 N 次结果完全相同）
3. 无进展检测（iteration 增加但 state 无实质变化）

State 命名空间：
    - stuck_detected : 是否检测到卡死
    - stuck_reason : 卡死原因描述
"""

from __future__ import annotations

import logging
from difflib import SequenceMatcher
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)


class StuckDetector(IOutputPlugin):
    """卡死检测 Output 插件。

    检测策略：
    1. 相同工具调用重复（连续 N 次相同 tool_name + args）
    2. 相同输出重复（连续 N 次结果完全相同）
    3. 无进展检测（iteration 增加但 state 无实质变化）

    Attributes:
        _window_size: 历史窗口大小（默认 5）
        _similarity_threshold: 相似度阈值（默认 0.9）
        _repeat_threshold: 重复次数阈值（默认 3）
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化卡死检测插件。

        Args:
            config: 插件配置字典，支持以下键：
                - window_size: 历史窗口大小（默认 5）
                - similarity_threshold: 相似度阈值（默认 0.9）
                - repeat_threshold: 重复次数阈值（默认 3）
        """
        self._config = config or {}
        self._window_size = self._config.get("window_size", 5)
        self._similarity_threshold = self._config.get("similarity_threshold", 0.9)
        self._repeat_threshold = self._config.get("repeat_threshold", 3)
        self._history: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "stuck_detector"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 15)

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """执行卡死检测。

        保存当前轮快照到历史窗口，依次检查工具调用重复
        和输出内容重复，检测到卡死时标记 state 并发布事件。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含卡死检测结果的输出结果
        """
        state = ctx.state
        snapshot = self._take_snapshot(state)

        # 维护滑动窗口
        self._history.append(snapshot)
        if len(self._history) > self._window_size:
            self._history = self._history[-self._window_size :]

        # 检查工具调用重复
        tool_reason = self._check_tool_repeat(self._history)

        # 检查输出重复
        output_reason = self._check_output_repeat(self._history)

        # 汇总卡死原因
        reasons = [r for r in (tool_reason, output_reason) if r]

        if reasons:
            combined_reason = "; ".join(reasons)
            logger.warning(
                "[%s] Stuck detected | reason=%s",
                self.name,
                combined_reason,
            )
            return OutputResult(
                state_updates={
                    "stuck_detected": True,
                    "stuck_reason": combined_reason,
                }
            )

        return OutputResult(
            state_updates={
                "stuck_detected": False,
                "stuck_reason": "",
            }
        )

    def _check_tool_repeat(self, history: list[dict[str, Any]]) -> str:
        """检查工具调用重复。

        从历史快照中提取连续的工具调用签名，若连续
        repeat_threshold 次相同则判定为卡死。

        Args:
            history: 历史快照列表

        Returns:
            卡死原因字符串，空字符串表示未检测到
        """
        if len(history) < self._repeat_threshold:
            return ""

        # 取最近 repeat_threshold 轮的工具调用签名
        recent = history[-self._repeat_threshold :]
        signatures = [h.get("tool_signature") for h in recent]

        # 所有签名必须相同且非空
        first_sig = signatures[0]
        if not first_sig:
            return ""

        if all(sig == first_sig for sig in signatures):
            return f"Tool call repeated {self._repeat_threshold} times: {first_sig[:100]}"

        return ""

    def _check_output_repeat(self, history: list[dict[str, Any]]) -> str:
        """检查输出重复。

        从历史快照中提取连续的输出内容，若连续
        repeat_threshold 次完全相同则判定为卡死。

        Args:
            history: 历史快照列表

        Returns:
            卡死原因字符串，空字符串表示未检测到
        """
        if len(history) < self._repeat_threshold:
            return ""

        recent = history[-self._repeat_threshold :]
        outputs = [h.get("result_text", "") for h in recent]

        first_output = outputs[0]
        if not first_output:
            return ""

        # 检查完全相同
        if all(out == first_output for out in outputs):
            return f"Output repeated {self._repeat_threshold} times identically"

        # 检查高度相似
        if all(self._compute_similarity(out, first_output) >= self._similarity_threshold for out in outputs[1:]):
            return f"Output repeated {self._repeat_threshold} times (similarity >= {self._similarity_threshold})"

        return ""

    def _take_snapshot(self, state: dict[str, Any]) -> dict[str, Any]:
        """从 state 提取关键信息作为快照。

        提取工具调用签名和输出文本，用于后续重复检测。

        Args:
            state: 管道当前状态字典

        Returns:
            包含 tool_signature 和 result_text 的快照字典
        """
        # 提取工具调用签名
        tool_calls = state.get(StateKeys.RAW_TOOL_CALLS, [])
        tool_parts: list[str] = []
        for tc in tool_calls:
            name = tc.get("name", "")
            args = tc.get("args", {})
            tool_parts.append(f"{name}({sorted(args.items())})")
        tool_signature = "|".join(tool_parts) if tool_parts else ""

        # 提取输出文本
        raw_result = state.get(StateKeys.RAW_RESULT)
        result_text = str(raw_result)[:500] if raw_result is not None else ""

        return {
            "iteration": state.get(StateKeys.ITERATION, 0),
            "tool_signature": tool_signature,
            "result_text": result_text,
        }

    def _compute_similarity(self, text1: str, text2: str) -> float:
        """计算两段文本的相似度。

        使用 difflib.SequenceMatcher 计算序列相似度。

        Args:
            text1: 第一段文本
            text2: 第二段文本

        Returns:
            相似度值 [0, 1]
        """
        if not text1 or not text2:
            return 0.0
        return SequenceMatcher(None, text1, text2).ratio()
