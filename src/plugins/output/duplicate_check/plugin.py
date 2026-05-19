"""重复检查 Output 插件 — 合并 duplicate_call + repetitive_output。

负责在管道循环的输出阶段检测工具调用重复和输出内容重复，
共享重复计数状态，超限时产出 end 路由信号。

合并收益：共享计数状态（router.duplicate_count / router.repetitive_count）+ 低维护成本。

M6d 阶段：从旧代码 agents/decision/strategies/iteration/ 中的
duplicate_call 和 repetitive_output 合并迁移。

State 命名空间：
    - router.duplicate_count : 工具调用重复计数（跨迭代）
    - router.repetitive_count : 输出内容重复计数（跨迭代）
    - router.last_tool_call : 上一次工具调用签名
    - router.last_response : 上一次 LLM 响应摘要
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import ErrorPolicy, RouteSignal, StateKeys

logger = logging.getLogger(__name__)


class DuplicateCheckPlugin(IOutputPlugin):
    """重复检查 Output 插件。

    合并了旧代码中 duplicate_call 和 repetitive_output 两个策略。
    两者都维护重复计数器，合并后共享 router.duplicate_count 命名空间。

    检查维度：
    1. 工具调用重复：相同工具+相同参数被连续调用
    2. 输出内容重复：LLM 连续返回相同或高度相似的内容

    优先级：4（系统级）
    错误策略：ABORT（重复检测异常必须终止管道）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.ABORT

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化重复检查插件。

        Args:
            config: 插件配置字典，支持以下键：
                - max_duplicate_calls: 最大工具调用重复次数（默认 3）
                - max_repetitive_output: 最大输出内容重复次数（默认 3）
                - similarity_threshold: 输出相似度阈值（默认 0.9）
        """
        self._config = config or {}
        self._max_duplicate_calls = self._config.get("max_duplicate_calls", 3)
        self._max_repetitive_output = self._config.get("max_repetitive_output", 3)
        self._similarity_threshold = self._config.get("similarity_threshold", 0.9)

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "duplicate_check"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 4)

    @property
    def route_signals(self) -> list[str]:
        """本插件可能产出的路由信号类型。"""
        return ["end"]

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """执行重复检查。

        检查工具调用和输出内容的重复度，超限时产出 end 信号。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含重复检查结果和路由信号的输出结果
        """
        result = await self._do_work(ctx)

        if result.get("__route_signal__"):
            signal = result.pop("__route_signal__")
            return OutputResult(state_updates=result, route_signal=signal)
        return OutputResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
        """执行重复检查逻辑。

        Args:
            ctx: 插件执行上下文

        Returns:
            重复检查结果字典
        """
        updates: dict[str, Any] = {}

        # 1. 工具调用重复检查
        dup_result = self._check_duplicate_calls(ctx)
        updates.update(dup_result)

        # 2. 输出内容重复检查
        rep_result = self._check_repetitive_output(ctx)
        updates.update(rep_result)

        # 3. 综合判断是否超限
        duplicate_count = updates.get("router.duplicate_count", ctx.state.get("router.duplicate_count", 0))
        repetitive_count = updates.get("router.repetitive_count", ctx.state.get("router.repetitive_count", 0))

        if duplicate_count > self._max_duplicate_calls:
            updates["__route_signal__"] = RouteSignal(
                route_type="end",
                reason=f"Duplicate tool calls exceeded: {duplicate_count} > {self._max_duplicate_calls}",
            )
            return updates

        if repetitive_count > self._max_repetitive_output:
            updates["__route_signal__"] = RouteSignal(
                route_type="end",
                reason=f"Repetitive output exceeded: {repetitive_count} > {self._max_repetitive_output}",
            )
            return updates

        return updates

    def _check_duplicate_calls(self, ctx: PluginContext) -> dict[str, Any]:
        """检查工具调用重复。

        通过对工具名+参数生成签名，与上一次工具调用签名对比，
        相同则增加重复计数。

        Args:
            ctx: 插件执行上下文

        Returns:
            重复检查结果字典
        """
        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        if not tool_calls:
            return {}

        # 生成当前工具调用签名
        current_signatures = []
        for tc in tool_calls:
            name = tc.get("name", "")
            args = tc.get("args", {})
            sig = hashlib.md5(f"{name}:{sorted(args.items())}".encode()).hexdigest()[:8]  # noqa: S324
            current_signatures.append(sig)

        current_sig = ",".join(current_signatures)
        last_sig = ctx.state.get("router.last_tool_call", "")

        # 对比
        duplicate_count = ctx.state.get("router.duplicate_count", 0)
        if current_sig and current_sig == last_sig:
            duplicate_count += 1
            logger.debug(
                "[%s] Duplicate tool call detected | count=%d",
                self.name, duplicate_count,
            )
        else:
            duplicate_count = 0  # 不同则重置

        return {
            "router.duplicate_count": duplicate_count,
            "router.last_tool_call": current_sig,
        }

    def _check_repetitive_output(self, ctx: PluginContext) -> dict[str, Any]:
        """检查输出内容重复。

        通过对 LLM 输出文本的前 N 个字符生成签名，
        与上一次输出对比，高度相似则增加重复计数。

        Args:
            ctx: 插件执行上下文

        Returns:
            重复检查结果字典
        """
        raw_result = ctx.state.get(StateKeys.RAW_RESULT)
        if raw_result is None:
            return {}

        # 生成当前输出签名（取前 500 字符）
        current_text = str(raw_result)[:500]
        current_hash = hashlib.md5(current_text.encode()).hexdigest()[:8]  # noqa: S324

        last_hash = ctx.state.get("router.last_response", "")

        # 对比
        repetitive_count = ctx.state.get("router.repetitive_count", 0)
        if current_hash and current_hash == last_hash:
            repetitive_count += 1
            logger.debug(
                "[%s] Repetitive output detected | count=%d",
                self.name, repetitive_count,
            )
        else:
            # 相似度检查（简单字符级对比）
            last_text = ctx.state.get("router.last_response_text", "")
            if last_text and self._compute_similarity(current_text, last_text) > self._similarity_threshold:
                repetitive_count += 1
                logger.debug(
                    "[%s] Similar output detected | similarity>%.2f | count=%d",
                    self.name, self._similarity_threshold, repetitive_count,
                )
            else:
                repetitive_count = 0  # 不同则重置

        return {
            "router.repetitive_count": repetitive_count,
            "router.last_response": current_hash,
            "router.last_response_text": current_text,
        }

    def _compute_similarity(self, text1: str, text2: str) -> float:
        """计算两段文本的相似度。

        使用简单的 Jaccard 相似度（基于字符 n-gram）。

        Args:
            text1: 第一段文本
            text2: 第二段文本

        Returns:
            相似度值 [0, 1]
        """
        if not text1 or not text2:
            return 0.0

        # 简单 word-level Jaccard
        words1 = set(text1.split())
        words2 = set(text2.split())

        if not words1 and not words2:
            return 1.0
        if not words1 or not words2:
            return 0.0

        intersection = words1 & words2
        union = words1 | words2
        return len(intersection) / len(union)
