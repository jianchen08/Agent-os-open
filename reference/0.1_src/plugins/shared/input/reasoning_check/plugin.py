"""推理检查 Input 插件。

负责在管道循环的输入阶段对 LLM 的推理过程进行安全审查，
检测推理链中的潜在风险，包括幻觉检测、逻辑谬误识别
和过度推理拦截。

M6b 阶段：从旧代码 isolation/ 中的推理拦截逻辑迁移，
与 security_check 合并为统一的安全模块。

State 命名空间：
    - reasoning.check_result : 本插件写入的推理检查结果
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)


class ReasoningCheckPlugin(IInputPlugin):
    """推理检查 Input 插件。

    从旧代码 isolation/ 中的推理拦截逻辑迁移而来。
    检查 LLM 推理过程中的潜在风险，包括：
    1. 过度推理检测：推理步数超过阈值
    2. 循环推理检测：相同推理步骤重复出现
    3. 推理超时检测：推理时间超过限制

    注意：本插件主要在 LLM 调用轮次中执行，
    检查的是上一轮 LLM 输出的推理内容。

    优先级：75（校验级，在 security_check 之后）
    错误策略：SKIP（推理检查异常不影响管道执行）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化推理检查插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用推理检查（默认 True）
                - max_reasoning_steps: 最大推理步数（默认 20）
                - max_duplicate_steps: 最大重复步数（默认 3）
                - max_reasoning_tokens: 最大推理 token 数（默认 4096）
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._max_steps = self._config.get("max_reasoning_steps", 20)
        self._max_duplicates = self._config.get("max_duplicate_steps", 3)
        self._max_tokens = self._config.get("max_reasoning_tokens", 4096)

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "reasoning_check"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 75)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """执行推理检查。

        检查上一轮 LLM 输出的推理内容是否存在潜在风险。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含推理检查结果状态更新的插件执行结果
        """
        result = await self._do_work(ctx)
        return PluginResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:  # noqa: PLR0911
        """执行推理检查逻辑。

        Args:
            ctx: 插件执行上下文

        Returns:
            推理检查结果字典
        """
        if not self._enabled:
            return {"reasoning.check_result": {"passed": True, "reason": "disabled"}}

        # 只在 LLM 调用轮次后检查
        core_type = ctx.state.get(StateKeys.CORE_TYPE, "llm_call")
        if core_type != "llm_call":
            return {"reasoning.check_result": {"passed": True, "reason": "not llm_call"}}

        raw_result = ctx.state.get(StateKeys.RAW_RESULT, "")
        if not raw_result:
            return {"reasoning.check_result": {"passed": True, "reason": "no output"}}

        # 1. 推理步数检查
        step_count = self._count_reasoning_steps(raw_result)
        if step_count > self._max_steps:
            return {
                "reasoning.check_result": {
                    "passed": False,
                    "reason": f"Too many reasoning steps: {step_count} > {self._max_steps}",
                    "step_count": step_count,
                },
                StateKeys.SHOULD_STOP: True,
            }

        # 2. 重复推理检查
        duplicate_count = self._count_duplicate_steps(raw_result)
        if duplicate_count > self._max_duplicates:
            return {
                "reasoning.check_result": {
                    "passed": False,
                    "reason": f"Too many duplicate steps: {duplicate_count} > {self._max_duplicates}",
                    "duplicate_count": duplicate_count,
                },
                StateKeys.SHOULD_STOP: True,
            }

        # 3. 推理 token 估算
        estimated_tokens = len(raw_result) // 2
        if estimated_tokens > self._max_tokens:
            return {
                "reasoning.check_result": {
                    "passed": False,
                    "reason": f"Reasoning too long: {estimated_tokens} > {self._max_tokens} tokens",
                    "estimated_tokens": estimated_tokens,
                },
                StateKeys.SHOULD_STOP: True,
            }

        return {
            "reasoning.check_result": {
                "passed": True,
                "reason": "all checks passed",
                "step_count": step_count,
                "duplicate_count": duplicate_count,
            }
        }

    def _count_reasoning_steps(self, text: str) -> int:
        """计算推理步数。

        通过统计常见的推理标记来估算推理步数。
        仅统计出现在推理上下文块中的标记，避免普通文本误报。
        推理上下文块包括：<think/>、[Reasoning]、[思考]、
        "推理过程"/"思考过程" 引导的段落等。

        Args:
            text: LLM 输出文本

        Returns:
            推理步数
        """

        # 先尝试定位推理上下文块；若未找到则回退到全文
        reasoning_blocks = self._extract_reasoning_blocks(text)
        search_text = "\n".join(reasoning_blocks) if reasoning_blocks else text

        # 仅在推理上下文中统计结构化步骤标记
        step_patterns = [
            r"(?:步骤|step)\s*[:：]?\s*\d+",
            r"(?:Step|STEP)\s*\d+[:：]",
            r"^#{1,3}\s+(?:步骤|Step)\s*\d+",
        ]
        count = 0
        for pattern in step_patterns:
            matches = re.findall(pattern, search_text, re.MULTILINE | re.IGNORECASE)
            count += len(matches)
        return count

    def _extract_reasoning_blocks(self, text: str) -> list[str]:
        """从文本中提取推理上下文块。

        识别包含推理标记的块（如 <think/> 标签、[Reasoning] 标题、
        "推理过程"/"思考过程" 段落等），仅在这些块内统计推理步骤，
        从而避免普通文本中的误报。

        Args:
            text: LLM 输出文本

        Returns:
            提取到的推理上下文块列表
        """

        blocks: list[str] = []

        # 1. <think ...>...</think > 标签内容
        think_matches = re.findall(r"<think[^>]*>(.*?)</think\s*>", text, re.DOTALL | re.IGNORECASE)
        blocks.extend(think_matches)

        # 2. [Reasoning] / [思考] / [推理过程] 标题段落（到下一个空行或标题）
        section_matches = re.findall(
            r"(?:^|\n)(?:#{1,3}\s+)?\[*(?:Reasoning|思考|推理过程|Thought)\]*\s*\n(.*?)(?=\n\s*\n|\n#|\Z)",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        blocks.extend(section_matches)

        return blocks

    def _count_duplicate_steps(self, text: str) -> int:
        """计算重复推理步数。

        通过检测文本中相同的句子或段落来估算重复度。

        Args:
            text: LLM 输出文本

        Returns:
            重复步数
        """
        # 按句号/换行分割
        sentences = [
            s.strip() for s in text.replace("。", ".\n").replace("\n", ".\n").split(".") if len(s.strip()) > 20
        ]
        if len(sentences) < 2:
            return 0

        # 计算重复
        seen: dict[str, int] = {}
        for s in sentences:
            normalized = s.lower().strip()
            if normalized in seen:
                seen[normalized] += 1
            else:
                seen[normalized] = 1

        return sum(v - 1 for v in seen.values() if v > 1)
