"""
重复工具调用检测器

从 execute_tools_node 和 should_continue 中提取的重复检测逻辑
负责检测重复的工具调用和连续的失败
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class DuplicateCallDetector:
    """
    重复工具调用检测器

    用于检测：
    1. 相同参数的重复工具调用（防止 LLM 循环调用）
    2. 连续的失败调用（防止无限重试）
    """

    DEFAULT_MAX_CONSECUTIVE_FAILURES = 2
    DEFAULT_MAX_SAME_PARAM_CALLS = 3

    @classmethod
    def _hash_inputs(cls, inputs: dict[str, Any] | None) -> str:
        """
        将 inputs 字典转换为可哈希的字符串

        Args:
            inputs: 工具输入参数

        Returns:
            参数的哈希字符串
        """
        if inputs is None:
            return "{}"
        try:
            return json.dumps(inputs, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(inputs)

    @classmethod
    def check_same_param_calls(
        cls,
        tool_calls_history: list[dict[str, Any]],
        max_calls: int = DEFAULT_MAX_SAME_PARAM_CALLS,
    ) -> dict[str, Any] | None:
        """
        检查最近 N 次调用是否使用相同参数

        只检测最近的调用，如果最近 N 次都是相同工具+相同参数，则触发。

        Args:
            tool_calls_history: 工具调用历史记录
            max_calls: 检测的最近调用次数（默认检测最近3次是否相同）

        Returns:
            如果检测到超限，返回包含详细信息的字典：
            - tool_name: 工具名称
            - inputs: 调用参数（LLM 原始参数）
            - call_count: 相同调用次数
            - max_allowed: 预设阈值
            如果没有超限，返回 None
        """
        if len(tool_calls_history) < max_calls:
            return None

        recent_calls = tool_calls_history[-max_calls:]

        first_call = recent_calls[0]
        first_tool_name = first_call.get("tool_name", "")
        first_llm_args = first_call.get("llm_args") or first_call.get("inputs")
        first_args_hash = cls._hash_inputs(first_llm_args)

        for call in recent_calls[1:]:
            tool_name = call.get("tool_name", "")
            llm_args = call.get("llm_args") or call.get("inputs")
            args_hash = cls._hash_inputs(llm_args)

            if tool_name != first_tool_name or args_hash != first_args_hash:
                return None

        logger.warning(
            f"[DuplicateCallDetector] 检测到最近 {max_calls} 次调用相同参数 | "
            f"tool_name={first_tool_name} | "
            f"llm_args={first_llm_args}"
        )
        return {
            "tool_name": first_tool_name,
            "inputs": first_llm_args,
            "call_count": max_calls,
            "max_allowed": max_calls,
        }

    @classmethod
    def check_duplicate(
        cls,
        tool_calls_history: list[dict[str, Any]],
        min_consecutive_calls: int = 2,
    ) -> dict[str, Any] | None:
        """
        检查是否有重复的工具调用（连续相同参数）

        检测最近 N 次调用中是否有相同工具名称和参数的重复调用

        Args:
            tool_calls_history: 工具调用历史记录
            min_consecutive_calls: 最小连续调用次数（默认2次）

        Returns:
            如果检测到重复，返回包含 tool_name 和 inputs 的字典
            如果没有检测到重复，返回 None
        """
        if len(tool_calls_history) < min_consecutive_calls:
            return None

        recent_calls = tool_calls_history[-min_consecutive_calls:]

        first_call = recent_calls[0]
        first_tool_name = first_call.get("tool_name")
        first_inputs = first_call.get("inputs")

        for call in recent_calls[1:]:
            if (
                call.get("tool_name") != first_tool_name
                or call.get("inputs") != first_inputs
            ):
                return None

        return {
            "tool_name": first_tool_name,
            "inputs": first_inputs,
            "consecutive_count": min_consecutive_calls,
        }

    @classmethod
    def get_consecutive_failures(
        cls,
        tool_calls_history: list[dict[str, Any]],
    ) -> dict[str, int]:
        """
        获取每个工具的连续失败次数

        从最近的调用开始统计，直到遇到成功的调用或不同的工具

        Args:
            tool_calls_history: 工具调用历史记录

        Returns:
            字典 {tool_name: consecutive_failure_count}
        """
        if not tool_calls_history:
            return {}

        consecutive_failures: dict[str, int] = {}
        last_tool_name: str | None = None

        # 从最近的调用开始检查
        for call in reversed(tool_calls_history):
            tool_name = call.get("tool_name")
            success = call.get("success", False)

            if not success:
                if tool_name == last_tool_name:
                    # 同一工具连续失败
                    consecutive_failures[tool_name] = (
                        consecutive_failures.get(tool_name, 0) + 1
                    )
                else:
                    # 不同工具的失败，开始新的计数
                    consecutive_failures[tool_name] = 1
                    last_tool_name = tool_name
            else:
                # 工具调用成功，停止统计
                break

        return consecutive_failures

    @classmethod
    def has_excessive_failures(
        cls,
        tool_calls_history: list[dict[str, Any]],
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    ) -> dict[str, Any] | None:
        """
        检查是否有工具连续失败超过限制

        Args:
            tool_calls_history: 工具调用历史记录
            max_consecutive_failures: 最大允许的连续失败次数

        Returns:
            如果有工具超过限制，返回包含 tool_name 和 failure_count 的字典
            如果没有超过限制，返回 None
        """
        consecutive_failures = cls.get_consecutive_failures(tool_calls_history)

        for tool_name, count in consecutive_failures.items():
            if count > max_consecutive_failures:
                return {
                    "tool_name": tool_name,
                    "failure_count": count,
                    "max_allowed": max_consecutive_failures,
                }

        return None

    @classmethod
    def check_all(
        cls,
        tool_calls_history: list[dict[str, Any]],
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
        max_same_param_calls: int = DEFAULT_MAX_SAME_PARAM_CALLS,
    ) -> dict[str, Any] | None:
        """
        执行所有检测

        检测优先级：
        1. 相同参数调用超限（最重要，防止 LLM 循环）
        2. 过度失败（防止无限重试）
        3. 连续重复调用（最近 N 次相同）

        Args:
            tool_calls_history: 工具调用历史记录
            max_consecutive_failures: 最大允许的连续失败次数
            max_same_param_calls: 相同参数的最大允许调用次数

        Returns:
            如果检测到任何问题，返回包含问题信息的字典
            如果没有问题，返回 None

        返回格式:
        - 相同参数超限: {"type": "same_param_exceeded", "tool_name": ..., "inputs": ..., "call_count": ...}
        - 过度失败: {"type": "excessive_failures", "tool_name": ..., "failure_count": ...}
        - 重复调用: {"type": "duplicate", "tool_name": ..., "inputs": ...}
        """
        same_param = cls.check_same_param_calls(
            tool_calls_history, max_same_param_calls
        )
        if same_param:
            return {
                "type": "same_param_exceeded",
                "tool_name": same_param["tool_name"],
                "inputs": same_param["inputs"],
                "call_count": same_param["call_count"],
                "max_allowed": same_param["max_allowed"],
            }

        excessive = cls.has_excessive_failures(
            tool_calls_history, max_consecutive_failures
        )
        if excessive:
            return {
                "type": "excessive_failures",
                "tool_name": excessive["tool_name"],
                "failure_count": excessive["failure_count"],
                "max_allowed": excessive["max_allowed"],
            }

        duplicate = cls.check_duplicate(tool_calls_history)
        if duplicate:
            return {
                "type": "duplicate",
                "tool_name": duplicate["tool_name"],
                "inputs": duplicate["inputs"],
                "consecutive_count": duplicate["consecutive_count"],
            }

        return None
