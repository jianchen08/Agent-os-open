"""输出重复守卫 Output 插件。

检测 LLM 输出重复，提供渐进式干预：
1. 1-2 次重复：丢弃输出 + 随机提示 + 路由 next_llm
2. 3 次重复：丢弃输出 + 更强提示 + 路由 next_llm
3. 4+ 次：产出 decision 信号
"""

from __future__ import annotations

import hashlib
import logging
import random
from difflib import SequenceMatcher
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import ErrorPolicy, RouteSignal, StateKeys

logger = logging.getLogger(__name__)

OUTPUT_REPEAT_PROMPTS_LIGHT = [
    "输出与之前相似，请尝试不同的解决方法。",
    "检测到重复输出，请换个思路继续。",
    "当前方法似乎没有进展，请尝试其他策略。",
    "请检查任务状态，可能需要调用工具获取新信息。",
]

OUTPUT_REPEAT_PROMPTS_STRONG = [
    "多次输出相似内容，请重新评估当前方案是否可行。",
    "似乎遇到了瓶颈，请考虑：1)任务是否已完成 2)是否需要更多信息 3)是否需要寻求帮助。",
    "持续重复输出表明当前策略无效，请尝试完全不同的方法。",
]


class OutputRepetitionGuard(IOutputPlugin):
    """输出重复守卫插件。

    渐进式干预策略：
    - 1-2 次：丢弃输出 + 轻度随机提示 + next_llm
    - 3 次：丢弃输出 + 重度随机提示 + next_llm
    - 4+ 次：产出 decision 信号

    优先级：12（Output 阶段，在 ErrorCheck 之后）
    错误策略：ABORT
    """

    error_policy = ErrorPolicy.ABORT

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._max_retries = self._config.get("max_retries", 3)
        self._similarity_threshold = self._config.get("similarity_threshold", 0.85)
        self._used_light_prompts: list[str] = []
        self._used_strong_prompts: list[str] = []

    @property
    def name(self) -> str:
        return "output_repetition_guard"

    @property
    def priority(self) -> int:
        return self._config.get("priority", 12)

    @property
    def route_signals(self) -> list[str]:
        return ["decision", "next_llm"]

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """执行输出重复检测，根据重复次数采取渐进式干预。"""
        result = await self._do_work(ctx)

        if result.get("__route_signal__"):
            signal = result.pop("__route_signal__")
            return OutputResult(state_updates=result, route_signal=signal)
        return OutputResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:  # noqa: PLR0911
        """核心工作逻辑，检测输出重复并生成对应的状态更新。"""
        # 管道已结束时跳过：post-end 阶段不应判定重复
        if ctx.state.get(StateKeys.ENDED, False):
            return {}

        # 仅在 llm_call 阶段判定：工具结果不是 LLM 输出，不参与重复判定。
        # 若在 tool_execute 阶段触发，工具结果文本会被误判为"输出重复"，
        # 并在 tool 消息后追加 system 提示，打断 assistant(tool_calls)→tool 序列。
        core_type = ctx.state.get(StateKeys.CORE_TYPE, "llm_call")
        if core_type != "llm_call":
            return {}

        raw_result = ctx.state.get(StateKeys.RAW_RESULT)
        if raw_result is None:
            return {}

        # 包含评估结论 JSON 的输出不应被判定为重复（即使文本相似）
        raw_text = str(raw_result)
        if "evaluation_result" in raw_text and '"passed"' in raw_text:
            return {
                "output.last_hash": hashlib.md5(raw_text[:500].encode()).hexdigest()[:8],
                "output.last_text": raw_text[:500],
                "output.repeat_count": 0,
            }

        current_text = str(raw_result)[:500]
        current_hash = hashlib.md5(current_text.encode()).hexdigest()[:8]

        last_hash = ctx.state.get("output.last_hash", "")
        last_text = ctx.state.get("output.last_text", "")
        repeat_count = ctx.state.get("output.repeat_count", 0)

        is_repeat = False
        if (
            current_hash == last_hash
            or last_text
            and self._compute_similarity(current_text, last_text) > self._similarity_threshold
        ):
            is_repeat = True

        if is_repeat:
            repeat_count += 1
        else:
            repeat_count = 0
            self._used_light_prompts = []
            self._used_strong_prompts = []

        updates = {
            "output.last_hash": current_hash,
            "output.last_text": current_text,
            "output.repeat_count": repeat_count,
        }

        if repeat_count == 0:
            return updates
        if repeat_count <= 2:
            prompt = self._get_random_prompt_light()
            updates[StateKeys.RAW_RESULT] = ""
            updates["messages"] = self._add_system_prompt(ctx, prompt)
            updates["__route_signal__"] = RouteSignal(
                route_type="next_llm",
                reason=f"Output repeat detected ({repeat_count}), retry with light prompt",
            )
            return updates
        if repeat_count <= self._max_retries:
            prompt = self._get_random_prompt_strong()
            updates[StateKeys.RAW_RESULT] = ""
            updates["messages"] = self._add_system_prompt(ctx, prompt)
            updates["__route_signal__"] = RouteSignal(
                route_type="next_llm",
                reason=f"Output repeat detected ({repeat_count}), retry with strong prompt",
            )
            return updates
        updates["__route_signal__"] = RouteSignal(
            route_type="decision",
            reason=f"Output repeat exceeded max retries ({self._max_retries})",
            payload={
                "decision_type": "agent",
                "repeat_count": repeat_count,
                "similarity_threshold": self._similarity_threshold,
                "used_light_prompts": self._used_light_prompts,
                "used_strong_prompts": self._used_strong_prompts,
                "suggested_action": "analyze_and_guide",
            },
        )
        return updates

    def _compute_similarity(self, text1: str, text2: str) -> float:
        """计算两段文本的相似度。

        Args:
            text1: 第一段文本。
            text2: 第二段文本。

        Returns:
            0.0 到 1.0 之间的相似度比值。
        """
        if not text1 or not text2:
            return 0.0
        return SequenceMatcher(None, text1, text2).ratio()

    def _get_random_prompt_light(self) -> str:
        """获取不重复的轻度随机提示词，用尽后重置。

        Returns:
            随机选择的轻度提示词字符串。
        """
        available = [p for p in OUTPUT_REPEAT_PROMPTS_LIGHT if p not in self._used_light_prompts]
        if not available:
            self._used_light_prompts = []
            available = OUTPUT_REPEAT_PROMPTS_LIGHT
        prompt = random.choice(available)
        self._used_light_prompts.append(prompt)
        return prompt

    def _get_random_prompt_strong(self) -> str:
        """获取不重复的重度随机提示词，用尽后重置。

        Returns:
            随机选择的重度提示词字符串。
        """
        available = [p for p in OUTPUT_REPEAT_PROMPTS_STRONG if p not in self._used_strong_prompts]
        if not available:
            self._used_strong_prompts = []
            available = OUTPUT_REPEAT_PROMPTS_STRONG
        prompt = random.choice(available)
        self._used_strong_prompts.append(prompt)
        return prompt

    def _add_system_prompt(self, ctx: PluginContext, prompt: str) -> list:
        """添加系统提示到消息列表。

        Args:
            ctx: 插件执行上下文。
            prompt: 要添加的提示词内容。

        Returns:
            添加了系统提示后的消息列表。
        """
        messages = list(ctx.state.get("messages", []))
        messages.append(
            {
                "role": "system",
                "content": f"[OutputRepetitionGuard] {prompt}",
            }
        )
        return messages
