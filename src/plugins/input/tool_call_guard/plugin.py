"""工具调用守卫 Input 插件。

检测工具调用重复，提供渐进式干预：
1. 单轮内重复：去重 + 提示
2. 多轮累积重复：丢弃 + 随机提示 + 重试（最多3次）
3. 超过阈值：产出 decision 信号
"""

from __future__ import annotations

import hashlib
import logging
import random
from typing import Any

from pipeline.plugin import IInputPlugin, PluginResult, PluginContext
from pipeline.types import ErrorPolicy, RouteSignal, StateKeys

logger = logging.getLogger(__name__)

TOOL_REPEAT_PROMPTS = [
    "检测到重复工具调用，上次结果已返回，请继续下一步。",
    "该工具已调用过，请基于已有结果继续。",
    "避免重复调用相同工具，尝试其他方法推进任务。",
    "重复调用不会获得新信息，请思考下一步行动。",
    "请检查任务状态，可能需要使用不同的工具。",
]


class ToolCallGuard(IInputPlugin):
    """工具调用守卫插件。

    渐进式干预策略：
    - 1-2 次重复：去重 + 随机提示
    - 3 次重复：丢弃工具调用 + 随机提示 + 标记重试
    - 4+ 次：产出 decision 信号

    优先级：15（Input 阶段，在 ParamInject 之后）
    错误策略：ABORT
    """

    error_policy = ErrorPolicy.ABORT

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._max_retries = self._config.get("max_retries", 3)
        self._used_prompts: list[str] = []

    @property
    def name(self) -> str:
        return "tool_call_guard"

    @property
    def priority(self) -> int:
        return self._config.get("priority", 15)

    @property
    def route_signals(self) -> list[str]:
        return ["decision", "next_llm"]

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """执行工具调用守卫检查，根据重复次数采取渐进式干预。"""
        result = await self._do_work(ctx)

        if result.get("__route_signal__"):
            signal = result.pop("__route_signal__")
            return PluginResult(state_updates=result, route_signal=signal)
        return PluginResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
        """核心工作逻辑，检测重复并生成对应的状态更新。"""
        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        if not tool_calls:
            return {}

        current_sig = self._generate_signature(tool_calls)
        last_sig = ctx.state.get("tool_call.last_signature", "")
        repeat_count = ctx.state.get("tool_call.repeat_count", 0)

        if current_sig and current_sig == last_sig:
            repeat_count += 1
        else:
            repeat_count = 0
            self._used_prompts = []

        updates = {
            "tool_call.last_signature": current_sig,
            "tool_call.repeat_count": repeat_count,
        }

        if repeat_count == 0:
            return updates
        elif repeat_count <= 2:
            prompt = self._get_random_prompt()
            updates["tool_call.filter_reason"] = f"Duplicate detected, using prompt: {prompt[:30]}..."
            updates["messages"] = self._add_system_prompt(ctx, prompt)
            return updates
        elif repeat_count <= self._max_retries:
            prompt = self._get_random_prompt()
            updates[StateKeys.RAW_TOOL_CALLS] = []
            updates["tool_call.blocked"] = True
            updates["tool_call.block_reason"] = f"Too many repeats ({repeat_count}), retry with prompt"
            updates["messages"] = self._add_system_prompt(ctx, prompt)
            updates["__route_signal__"] = RouteSignal(
                route_type="next_llm",
                reason=f"Tool call blocked after {repeat_count} repeats",
            )
            return updates
        else:
            updates["__route_signal__"] = RouteSignal(
                route_type="decision",
                reason=f"Tool call repeat exceeded max retries ({self._max_retries})",
                payload={
                    "decision_type": "agent",
                    "repeat_count": repeat_count,
                    "tool_signature": current_sig,
                    "used_prompts": self._used_prompts,
                    "suggested_action": "analyze_and_guide",
                },
            )
            return updates

    def _generate_signature(self, tool_calls: list[dict]) -> str:
        """生成工具调用签名，用于检测重复调用。

        Args:
            tool_calls: 工具调用列表，每个元素包含 name 和 args。

        Returns:
            逗号分隔的签名哈希字符串。
        """
        signatures = []
        for tc in tool_calls:
            name = tc.get("name", "")
            args = tc.get("args", {})
            sig = hashlib.md5(f"{name}:{sorted(args.items())}".encode()).hexdigest()[:8]
            signatures.append(sig)
        return ",".join(signatures)

    def _get_random_prompt(self) -> str:
        """获取不重复的随机提示词，用尽后重置。

        Returns:
            随机选择的提示词字符串。
        """
        available = [p for p in TOOL_REPEAT_PROMPTS if p not in self._used_prompts]
        if not available:
            self._used_prompts = []
            available = TOOL_REPEAT_PROMPTS
        prompt = random.choice(available)
        self._used_prompts.append(prompt)
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
        messages.append({
            "role": "system",
            "content": f"[ToolCallGuard] {prompt}",
        })
        return messages
