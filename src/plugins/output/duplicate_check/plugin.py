"""重复检查 Output 插件 — 合并 duplicate_call + repetitive_output。

负责在管道循环的输出阶段检测工具调用重复和输出内容重复，
采用三级渐进策略：软提示 → 拦截重路由 → 终止管道。

合并收益：共享重复计数状态（router.duplicate_count / router.repetitive_count）+ 低维护成本。

M6d 阶段：从旧代码 agents/decision/strategies/iteration/ 中的
duplicate_call 和 repetitive_output 合并迁移。

策略说明：
    - 第一级（count < max）：注入软提示，工具调用仍执行
    - 第二级（count >= max）：移除重复调用 + 注入强警告 + 路由回 LLM
    - 第三级（拦截次数 >= hard_limit）：终止管道
      - 主 agent：注入用户通知消息后终止
      - 子 agent：直接终止（任务失败会通知上级）

State 命名空间：
    - router.duplicate_count : 工具调用重复计数（跨迭代）
    - router.repetitive_count : 输出内容重复计数（跨迭代）
    - router.duplicate_intercepts : 拦截总次数
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

_HINT_TEMPLATES = {
    1: "你已经连续 {count} 次使用 {tool} 执行相同操作，结果不会有变化。请考虑换一种方式完成任务。",
    2: "你仍然在重复调用 {tool}，这已经是第 {count} 次了。请立即停止使用该工具和参数，尝试完全不同的方法。",
}

_MAIN_AGENT_TERMINATE_MSG = (
    "抱歉，我在执行过程中陷入了重复调用同一工具的死循环，无法继续完成当前任务。"
    "请提供更多指示或调整任务要求，我将重新尝试。"
)


class DuplicateCheckPlugin(IOutputPlugin):
    """重复检查 Output 插件。

    合并了旧代码中 duplicate_call 和 repetitive_output 两个策略。
    两者都维护重复计数器，合并后共享 router.duplicate_count 命名空间。

    检查维度：
    1. 工具调用重复：相同工具+相同参数被连续调用
    2. 输出内容重复：LLM 连续返回相同或高度相似的内容

    三级渐进策略：
    - 第一级：注入软提示（工具调用仍执行）
    - 第二级：移除重复调用 + 强警告 + 路由回 LLM
    - 第三级：终止管道（主 agent 通知用户，子 agent 直接终止）

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
                - max_duplicate_calls: 工具调用重复拦截阈值（默认 3）
                - max_repetitive_output: 输出内容重复拦截阈值（默认 3）
                - hard_limit_intercepts: 拦截次数硬上限，达到后终止管道（默认 4）
                - similarity_threshold: 输出相似度阈值（默认 0.9）
        """
        self._config = config or {}
        self._max_duplicate_calls = self._config.get("max_duplicate_calls", 3)
        self._max_repetitive_output = self._config.get("max_repetitive_output", 3)
        self._hard_limit_intercepts = self._config.get("hard_limit_intercepts", 4)
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
        return ["next_llm", "end"]

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """执行重复检查。

        采用三级渐进策略处理重复。

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

        # 3. 综合判断
        duplicate_count = updates.get("router.duplicate_count", ctx.state.get("router.duplicate_count", 0))
        repetitive_count = updates.get("router.repetitive_count", ctx.state.get("router.repetitive_count", 0))

        # 3a. 工具调用重复处理
        if duplicate_count > 0:
            return self._handle_duplicate_tool_calls(ctx, updates, duplicate_count)

        # 3b. 输出内容重复处理
        if repetitive_count > 0:
            return self._handle_repetitive_output(ctx, updates, repetitive_count)

        return updates

    def _handle_duplicate_tool_calls(
        self, ctx: PluginContext, updates: dict[str, Any], count: int,
    ) -> dict[str, Any]:
        """处理工具调用重复，三级渐进策略。

        Args:
            ctx: 插件执行上下文
            updates: 已有的状态更新字典
            count: 当前重复计数

        Returns:
            更新后的状态字典
        """
        tool_desc = self._build_tool_call_description(ctx)
        intercepts = ctx.state.get("router.duplicate_intercepts", 0)

        # 第三级：拦截次数达到硬上限 → 终止管道
        if intercepts >= self._hard_limit_intercepts:
            return self._terminate_pipeline(ctx, updates, tool_desc, intercepts)

        # 第二级：重复达到阈值 → 拦截 + 路由回 LLM
        if count >= self._max_duplicate_calls:
            warning = (
                f"检测到重复工具调用{tool_desc}，已跳过执行。"
                f"请不要再次使用相同的工具和参数，请尝试其他方法。"
            )
            self._inject_warning(ctx, warning)
            updates[StateKeys.RAW_TOOL_CALLS] = []
            updates["router.duplicate_count"] = 0
            updates["router.duplicate_intercepts"] = intercepts + 1
            logger.info(
                "[%s] Duplicate tool calls intercepted | count=%d intercepts=%d tool=%s",
                self.name, count, intercepts + 1, tool_desc,
            )
            updates["__route_signal__"] = RouteSignal(
                route_type="next_llm",
                reason=f"Duplicate tool calls intercepted ({count}): {tool_desc}",
            )
            return updates

        # 第一级：早期重复 → 注入软提示，工具调用仍执行
        hint = self._build_hint(count, tool_desc)
        self._inject_hint(ctx, hint)
        logger.info(
            "[%s] Duplicate tool call soft hint | count=%d tool=%s",
            self.name, count, tool_desc,
        )
        return updates

    def _handle_repetitive_output(
        self, ctx: PluginContext, updates: dict[str, Any], count: int,
    ) -> dict[str, Any]:
        """处理输出内容重复，三级渐进策略。

        Args:
            ctx: 插件执行上下文
            updates: 已有的状态更新字典
            count: 当前重复计数

        Returns:
            更新后的状态字典
        """
        intercepts = ctx.state.get("router.duplicate_intercepts", 0)

        # 第三级：拦截次数达到硬上限 → 终止管道
        if intercepts >= self._hard_limit_intercepts:
            return self._terminate_pipeline(ctx, updates, "重复输出", intercepts)

        # 第二级：重复达到阈值 → 清空输出 + 路由回 LLM
        if count >= self._max_repetitive_output:
            warning = "检测到重复输出相似内容，请尝试其他方法或给出不同的回复。"
            self._inject_warning(ctx, warning)
            updates[StateKeys.RAW_RESULT] = ""
            updates["router.repetitive_count"] = 0
            updates["router.duplicate_intercepts"] = intercepts + 1
            logger.info(
                "[%s] Repetitive output intercepted | count=%d intercepts=%d",
                self.name, count, intercepts + 1,
            )
            updates["__route_signal__"] = RouteSignal(
                route_type="next_llm",
                reason=f"Repetitive output intercepted ({count})",
            )
            return updates

        # 第一级：早期重复 → 注入软提示
        hint = f"你已经连续 {count} 次输出相似内容，请尝试换一种方式回复。"
        self._inject_hint(ctx, hint)
        logger.info(
            "[%s] Repetitive output soft hint | count=%d",
            self.name, count,
        )
        return updates

    def _terminate_pipeline(
        self, ctx: PluginContext, updates: dict[str, Any], desc: str, intercepts: int,
    ) -> dict[str, Any]:
        """终止管道，主 agent 注入用户通知，子 agent 直接终止。

        Args:
            ctx: 插件执行上下文
            updates: 已有的状态更新字典
            desc: 重复描述
            intercepts: 当前拦截次数

        Returns:
            包含终止路由信号的更新字典
        """
        agent_level = ctx.state.get(StateKeys.AGENT_LEVEL, "L1")
        is_main = agent_level in ("L1", "L1_MAIN") or ctx.state.get("delegate_depth", 0) == 0

        if is_main:
            messages = list(ctx.state.get("messages", []))
            messages.append({"role": "assistant", "content": _MAIN_AGENT_TERMINATE_MSG})
            ctx.state["messages"] = messages
            logger.warning(
                "[%s] Pipeline terminating (main agent) | intercepts=%d desc=%s",
                self.name, intercepts, desc,
            )
        else:
            logger.warning(
                "[%s] Pipeline terminating (sub agent) | intercepts=%d desc=%s",
                self.name, intercepts, desc,
            )

        updates["__route_signal__"] = RouteSignal(
            route_type="end",
            reason=f"Duplicate intercepts exceeded hard limit ({intercepts}): {desc}",
        )
        return updates

    def _build_hint(self, count: int, tool_desc: str) -> str:
        """构建早期软提示消息。

        Args:
            count: 当前重复计数
            tool_desc: 工具调用描述

        Returns:
            软提示消息字符串
        """
        template = _HINT_TEMPLATES.get(count, _HINT_TEMPLATES[max(_HINT_TEMPLATES)])
        return template.format(count=count, tool=tool_desc)

    def _build_tool_call_description(self, ctx: PluginContext) -> str:
        """构建工具调用的可读描述。

        Args:
            ctx: 插件执行上下文

        Returns:
            工具调用描述字符串，如 "file_read(path=xxx)"
        """
        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        if not tool_calls:
            return ""
        parts = []
        for tc in tool_calls:
            name = tc.get("name", "unknown")
            args = tc.get("args") or tc.get("arguments", {})
            if isinstance(args, str):
                parts.append(f"{name}({args})")
            else:
                args_str = ", ".join(f"{k}={v}" for k, v in args.items())
                parts.append(f"{name}({args_str})")
        return "、".join(parts)

    def _inject_warning(self, ctx: PluginContext, message: str) -> None:
        """向消息列表注入强警告（第二级拦截时使用）。

        Args:
            ctx: 插件执行上下文
            message: 警告消息内容
        """
        messages = list(ctx.state.get("messages", []))
        messages.append({"role": "system", "content": f"[DuplicateCheck] {message}"})
        ctx.state["messages"] = messages

    def _inject_hint(self, ctx: PluginContext, message: str) -> None:
        """向消息列表注入软提示（第一级早期提示时使用）。

        Args:
            ctx: 插件执行上下文
            message: 提示消息内容
        """
        messages = list(ctx.state.get("messages", []))
        messages.append({"role": "system", "content": f"[DuplicateCheck] {message}"})
        ctx.state["messages"] = messages

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
            raw_args = tc.get("args") or tc.get("arguments", {})
            if isinstance(raw_args, str):
                sig = hashlib.md5(f"{name}:{raw_args}".encode()).hexdigest()[:8]  # noqa: S324
            else:
                sig = hashlib.md5(f"{name}:{sorted(raw_args.items())}".encode()).hexdigest()[:8]  # noqa: S324
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
