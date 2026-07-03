"""错误检查 Output 插件。

负责在管道循环的输出阶段检查 Core 执行结果中的错误，
判断是否可重试，并产出相应的路由信号。

M6d 阶段：从旧代码 agents/decision/strategies/iteration/error_check_strategy 迁移。
职责分层：基础设施错误（网络/超时）→ Core 装饰器重试；
业务错误（格式/空响应）→ 本插件产出路由信号。

State 命名空间：
    - error_analysis : 本插件写入的错误分析结果
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import ErrorPolicy, RouteSignal, StateKeys

logger = logging.getLogger(__name__)


class ErrorCheckPlugin(IOutputPlugin):
    """错误检查 Output 插件。

    从旧代码 error_check_strategy 迁移而来。检查 Core 执行结果
    中的错误，判断错误类型和可重试性，产出路由信号。

    错误分层处理：
    - 基础设施错误（网络/超时）：由 Core 内部重试处理
    - 业务错误（空响应/格式错误）：本插件分析并决策
    - 不可重试错误：产出 end 信号
    - 可重试错误：产出 next_llm 信号 + 增加 retry.count

    优先级：2（系统级，仅次于 stop_check）
    错误策略：ABORT（错误检查异常必须终止管道）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.ABORT

    # 空响应指示词
    _EMPTY_RESPONSE_INDICATORS = {"", "none", "null", "undefined", "无", "空"}

    # 工具缺失错误关键词
    _TOOL_MISSING_KEYWORDS = [
        "tool",
        "not found",
        "not registered",
        "not available",
        "unknown function",
        "no such tool",
        "doesn't exist",
        "工具",
        "未找到",
        "未注册",
        "不存在",
    ]

    # 知识不足指示关键词（出现在 LLM 回复中）
    _KNOWLEDGE_INSUFFICIENT_KEYWORDS = [
        "i don't know",
        "i cannot",
        "i'm unable",
        "i am unable",
        "no information",
        "not enough information",
        "insufficient data",
        "我不知道",
        "无法回答",
        "没有足够",
        "信息不足",
    ]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化错误检查插件。

        Args:
            config: 插件配置字典，支持以下键：
                - max_retries: 业务错误最大重试次数（默认 3）
                - transient_max_retries: 临时错误最大重试次数（默认 10）
                - check_empty_response: 是否检查空响应（默认 True）
                - check_format_error: 是否检查格式错误（默认 True）
                - check_tool_missing: 是否检查工具缺失（默认 True）
                - check_knowledge_insufficient: 是否检查知识不足（默认 True）
                - check_strategy_error: 是否检查策略错误（默认 True）
        """
        self._config = config or {}
        self._max_retries = self._config.get("max_retries", 3)
        # 临时错误（network/timeout/503/429）单独计数并重试：临时错误重试上限
        # 高于一般错误（默认 10 次，覆盖一般上游抖动 ~10-20 分钟），仍失败则直接
        # failed，让父任务走正常的 child_terminal 通知 → retry / 上抛失败链。
        # 不采用"耗尽后挂起等恢复"的策略，否则 wait_event 没有主动唤醒源，
        # 上游恢复后无人 set 会导致整条 pipeline 死挂。
        self._transient_max_retries = self._config.get("transient_max_retries", 10)
        self._check_empty = self._config.get("check_empty_response", True)
        self._check_format = self._config.get("check_format_error", True)
        self._check_tool_missing = self._config.get("check_tool_missing", True)
        self._check_knowledge_insufficient = self._config.get(
            "check_knowledge_insufficient",
            True,
        )
        self._check_strategy_error = self._config.get("check_strategy_error", True)

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "error_check"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 2)

    @property
    def route_signals(self) -> list[str]:
        """本插件可能产出的路由信号类型。"""
        return ["end", "next_llm", "wait"]

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """执行错误检查。

        检查 Core 执行结果中的错误，判断可重试性并产出路由信号。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含错误分析和路由信号的输出结果
        """
        result = await self._do_work(ctx)

        if result.get("__route_signal__"):
            signal = result.pop("__route_signal__")
            return OutputResult(state_updates=result, route_signal=signal)
        return OutputResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:  # noqa: PLR0911
        """执行错误检查逻辑。

        检查顺序：raw_error → tool_missing → empty_response →
        knowledge_insufficient → format_error → strategy_error → 无错误。

        Args:
            ctx: 插件执行上下文

        Returns:
            错误分析结果字典
        """
        # 检查 Core 原始错误（含 tool_missing 诊断）
        raw_error = ctx.state.get(StateKeys.RAW_ERROR)
        if raw_error is not None:
            return self._handle_raw_error(ctx, raw_error)

        # 检查空响应（但 LLM 返回 tool_calls 时不算空响应）
        if self._check_empty:
            raw_result = ctx.state.get(StateKeys.RAW_RESULT)
            raw_tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
            if not raw_tool_calls and self._is_empty_response(raw_result):
                # 空响应直接归 empty_response（它有自己的重试语义）。
                # 不在此用"无记忆/知识"抢判 knowledge_insufficient：默认配置下两个
                # state 恒空，会导致空响应永远被吞、empty_response 成死分支。
                # knowledge_insufficient 仅在 LLM 显式声明"我不知道"时判定（见下）。
                return self._handle_empty_response(ctx)

        # 检查 LLM 回复中的知识不足指示
        if self._check_knowledge_insufficient:
            raw_result = ctx.state.get(StateKeys.RAW_RESULT)
            if raw_result and self._is_knowledge_insufficient_response(raw_result):  # noqa: SIM102
                # 结合记忆上下文判断
                if self._is_knowledge_insufficient(ctx):
                    return self._handle_knowledge_insufficient(ctx)

        # 检查格式错误（但 LLM 返回 tool_calls 时不算格式错误）
        if self._check_format:
            core_type = ctx.state.get(StateKeys.CORE_TYPE, "")
            raw_result = ctx.state.get(StateKeys.RAW_RESULT)
            raw_tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
            if core_type != "tool_execute" and not raw_tool_calls and raw_result and self._is_format_error(raw_result):
                # 格式错误 + 反复重试 → 可能是策略错误
                if self._check_strategy_error and self._is_strategy_error(ctx):
                    return self._handle_strategy_error(ctx)
                return self._handle_format_error(ctx)

        # 检查策略错误：多次重试仍然失败
        if self._check_strategy_error and self._is_strategy_error(ctx):
            return self._handle_strategy_error(ctx)

        # 无错误
        # 重置连续错误追踪
        return {
            StateKeys.EXECUTION_STATUS: "success",
            StateKeys.ERROR_ANALYSIS: None,
            "error_check.last_error_type": "",
            "error_check.consecutive_same_type": 0,
        }

    def _handle_raw_error(self, ctx: PluginContext, error: Any) -> dict[str, Any]:
        """处理 Core 原始错误。

        根据错误内容判断诊断类别：
        - tool_missing: 工具不存在或未注册
        - 其他: core_error（网络/超时/认证等）

        错误处理分层：
        - **临时错误**（service_down / rate_limit / network / server_error）：
          走独立计数 `retry.transient_count`，next_llm 重试 _transient_max_retries
          次（默认 10）。耗尽后 **直接 failed**（route=end），由 task 失败链
          通知父任务。不走 route=wait——wait 没有主动唤醒源，会无限挂起。
        - **业务可重试错误**（empty_response / format_error 等）：
          走 _max_retries（默认 3），next_llm 重试。
        - **永久错误**（auth/quota/bad_request/strategy_error）：
          保持原 end 行为，这些是 LLM 无法自行恢复的。

        Args:
            ctx: 插件执行上下文
            error: 原始错误对象

        Returns:
            错误分析结果字典
        """
        error_str = str(error)
        retry_count = ctx.state.get("retry.count", 0)
        transient_count = ctx.state.get("retry.transient_count", 0)

        category = "core_error"
        if self._check_tool_missing and self._is_tool_missing_error(error_str):
            category = "tool_missing"
        elif "所有工具执行失败" in error_str:
            category = "all_tools_failed"

        # 用统一分类器判断是否为临时错误（service_down/rate_limit/network/server_error）
        is_transient = self._classify_transient(error)

        retryable = self._is_retryable_error(error_str, category)
        consecutive = self._track_consecutive_error(ctx, category)
        analysis = {
            "retryable": retryable,
            "reason": error_str[:200],
            "category": category,
            "retry_count": retry_count,
            "transient": is_transient,
            "transient_count": transient_count,
        }

        # 临时错误单独走更大重试上限的路径（默认 10 次），上游短暂抖动期间持续重试。
        # 与业务错误的 retry.count 分开计数，避免互相干扰。
        if is_transient and retryable:
            if transient_count < self._transient_max_retries:
                return {
                    StateKeys.EXECUTION_STATUS: "needs_retry",
                    StateKeys.ERROR_ANALYSIS: analysis,
                    "retry.transient_count": transient_count + 1,
                    "error_check.last_error_type": category,
                    "error_check.consecutive_same_type": consecutive,
                    "__route_signal__": RouteSignal(
                        route_type="next_llm",
                        reason=(
                            f"Transient {category} "
                            f"(attempt {transient_count + 1}/"
                            f"{self._transient_max_retries}): "
                            f"{error_str[:100]}"
                        ),
                    ),
                }
            # 临时错误重试到上限：直接 failed，由 task 失败链通知父任务。
            # 不再 route=wait 挂起等"恢复"——wait 没有主动唤醒源会死挂。
            logger.warning(
                "[error_check] 临时错误重试上限耗尽，置为 failed (category=%s transient=%d/%d): %s",
                category,
                transient_count,
                self._transient_max_retries,
                error_str[:150],
            )
            return {
                StateKeys.EXECUTION_STATUS: "failed",
                StateKeys.ERROR_ANALYSIS: analysis,
                "error_check.last_error_type": category,
                "error_check.consecutive_same_type": consecutive,
                "__route_signal__": RouteSignal(
                    route_type="end",
                    reason=(
                        f"Transient {category} exhausted "
                        f"({transient_count}/"
                        f"{self._transient_max_retries}): "
                        f"{error_str[:100]}"
                    ),
                ),
            }

        if retryable and retry_count < self._max_retries:
            # 非临时但可重试的业务错误：走原 max_retries 路径
            return {
                StateKeys.EXECUTION_STATUS: "needs_retry",
                StateKeys.ERROR_ANALYSIS: analysis,
                "retry.count": retry_count + 1,
                "error_check.last_error_type": category,
                "error_check.consecutive_same_type": consecutive,
                "__route_signal__": RouteSignal(
                    route_type="next_llm",
                    reason=(f"Retryable {category} (attempt {retry_count + 1}/{self._max_retries}): {error_str[:100]}"),
                ),
            }

        # 永久不可重试错误（auth/quota/bad_request/strategy）或重试用尽：产出 end 信号
        return {
            StateKeys.EXECUTION_STATUS: "failed",
            StateKeys.ERROR_ANALYSIS: analysis,
            "error_check.last_error_type": category,
            "error_check.consecutive_same_type": consecutive,
            "__route_signal__": RouteSignal(
                route_type="end",
                reason=(f"Non-retryable {category} or max retries reached: {error_str[:100]}"),
            ),
        }

    @staticmethod
    def _classify_transient(error: Any) -> bool:
        """判断错误是否为临时性（可等待恢复）错误。

        用 llm.error_classifier.classify_error 做统一分类，避免在本插件里
        重复嗅探异常字符串。临时错误种类：
        - service_down (503)
        - rate_limit (429)
        - network (超时/连接失败)
        - server_error (500)

        这些错误的特点是「不是 LLM/任务本身的问题，是上游临时抖动」，
        按"错误就等下再调用"的原则处理，由 idle 总超时兜底。

        Args:
            error: 错误对象（可能是异常、字符串，或 state 里存的
                   "ExceptionType: message" 格式字符串）

        Returns:
            是否为临时错误
        """
        try:
            from llm.error_classifier import classify_error  # noqa: PLC0415

            # 优先用真实异常对象分类（类型名匹配最准）
            if isinstance(error, BaseException):
                info = classify_error(error)
                return info.kind.value in (
                    "service_down",
                    "rate_limit",
                    "network",
                    "server_error",
                )

            # state["RAW_ERROR"] 存的是字符串。错误消息常以异常类型名开头
            # （"ServiceUnavailableError: ..."）。解析出类型名后用 type()
            # 动态构造一个同名异常，让分类器走类型名分支精确匹配。
            msg = str(error)
            type_prefix = ""
            if ":" in msg:
                type_prefix = msg.split(":", 1)[0].strip()

            def _is_valid_type_name(name: str) -> bool:
                """判断字符串是否像合法的 Python 异常类型名（首字母大写、无空格）。"""
                return bool(name) and name[0].isupper() and " " not in name

            if type_prefix and _is_valid_type_name(type_prefix):
                # 动态创建一个名字正确的异常子类，使 type(exc).__name__ 命中
                dyn_exc_cls = type(type_prefix, (Exception,), {})
                info = classify_error(dyn_exc_cls(msg))
                kind = info.kind.value
                if kind in ("service_down", "rate_limit", "network", "server_error"):
                    return True

            # 兜底：分类器未识别时，对几个高置信度的临时关键词做本地嗅探，
            # 避免把明显的 503/超时/限流漏判成永久错误。
            msg_lower = msg.lower()
            transient_markers = (
                "service temporarily unavailable",
                "service unavailable",
                "503",
                "502 bad gateway",
                "bad gateway",
                "rate limit",
                "rate_limit",
                "429",
                "timeout",
                "timed out",
                "reading data from socket",
                "connection reset",
                "temporarily",
            )
            return any(m in msg_lower for m in transient_markers)
        except Exception:
            # 分类器不可用时保守按非临时处理，回退到原 end 逻辑
            return False

    def _handle_empty_response(self, ctx: PluginContext) -> dict[str, Any]:
        """处理空响应。

        Args:
            ctx: 插件执行上下文

        Returns:
            错误分析结果字典
        """
        retry_count = ctx.state.get("retry.count", 0)
        category = "empty_response"
        consecutive = self._track_consecutive_error(ctx, category)
        analysis = {
            "retryable": True,
            "reason": "Empty response from LLM",
            "category": category,
            "retry_count": retry_count,
        }

        if retry_count < self._max_retries:
            return {
                StateKeys.EXECUTION_STATUS: "needs_retry",
                StateKeys.ERROR_ANALYSIS: analysis,
                "retry.count": retry_count + 1,
                "error_check.last_error_type": category,
                "error_check.consecutive_same_type": consecutive,
                "__route_signal__": RouteSignal(
                    route_type="next_llm",
                    reason=(f"Empty response, retry {retry_count + 1}/{self._max_retries}"),
                ),
            }
        return {
            StateKeys.EXECUTION_STATUS: "failed",
            StateKeys.ERROR_ANALYSIS: analysis,
            "error_check.last_error_type": category,
            "error_check.consecutive_same_type": consecutive,
            "__route_signal__": RouteSignal(
                route_type="end",
                reason="Empty response after max retries",
            ),
        }

    def _handle_format_error(self, ctx: PluginContext) -> dict[str, Any]:
        """处理格式错误。

        Args:
            ctx: 插件执行上下文

        Returns:
            错误分析结果字典
        """
        retry_count = ctx.state.get("retry.count", 0)
        category = "format_error"
        consecutive = self._track_consecutive_error(ctx, category)
        analysis = {
            "retryable": True,
            "reason": "Response format error",
            "category": category,
            "retry_count": retry_count,
        }

        if retry_count < self._max_retries:
            return {
                StateKeys.EXECUTION_STATUS: "needs_retry",
                StateKeys.ERROR_ANALYSIS: analysis,
                "retry.count": retry_count + 1,
                "error_check.last_error_type": category,
                "error_check.consecutive_same_type": consecutive,
                "__route_signal__": RouteSignal(
                    route_type="next_llm",
                    reason=(f"Format error, retry {retry_count + 1}/{self._max_retries}"),
                ),
            }
        return {
            StateKeys.EXECUTION_STATUS: "failed",
            StateKeys.ERROR_ANALYSIS: analysis,
            "error_check.last_error_type": category,
            "error_check.consecutive_same_type": consecutive,
            "__route_signal__": RouteSignal(
                route_type="end",
                reason="Format error after max retries",
            ),
        }

    def _is_empty_response(self, result: Any) -> bool:
        """检查是否为空响应。

        Args:
            result: Core 输出结果

        Returns:
            是否为空响应
        """
        if result is None:
            return True
        return bool(isinstance(result, str) and result.strip().lower() in self._EMPTY_RESPONSE_INDICATORS)

    def _track_consecutive_error(
        self,
        ctx: PluginContext,
        category: str,
    ) -> int:
        """追踪连续相同类型的错误次数。

        当错误类型与上一次相同时递增计数器，
        否则重置为 1。用于策略错误检测，
        避免不同类型重试的误判。

        Args:
            ctx: 插件执行上下文
            category: 当前错误类别

        Returns:
            更新后的连续相同类型错误次数
        """
        last_type = ctx.state.get(
            "error_check.last_error_type",
            "",
        )
        consecutive = ctx.state.get(
            "error_check.consecutive_same_type",
            0,
        )
        if category == last_type:
            consecutive += 1
        else:
            consecutive = 1
        return consecutive

    def _is_format_error(self, result: Any) -> bool:
        """检查是否为格式错误（代码块未正确关闭）。

        通过统计 ``` 出现次数判断：每个代码块需要一对 ```，
        总数必须是偶数。奇数表示有未关闭的代码块。

        Args:
            result: Core 输出结果

        Returns:
            是否为格式错误
        """
        if not isinstance(result, str):
            return False
        tick_count = result.count("```")
        # 偶数个 ``` = 代码块正确开闭；奇数 = 有未关闭的块
        return tick_count % 2 != 0

    def _is_retryable_error(self, error_str: str, category: str = "core_error") -> bool:
        """判断错误是否可重试。

        tool_missing 通常不可重试（除非工具可能后续注册），
        其他非认证类错误可重试。

        Args:
            error_str: 错误信息字符串
            category: 错误类别

        Returns:
            是否可重试
        """
        # 工具缺失：标记为可重试，因为工具可能在后续管道中注册
        if category == "tool_missing":
            return True
        # 所有工具执行失败：标记为可重试，LLM 可降级处理（用已有知识回答等）
        if category == "all_tools_failed":
            return True
        # 知识不足：可重试（可能通过知识注入改善）
        if category == "knowledge_insufficient":
            return True
        # 策略错误：标记为不可重试（需要调整策略而非重试相同路径）
        if category == "strategy_error":
            return False
        non_retryable_keywords = ["auth", "permission", "forbidden", "invalid api key", "quota"]
        error_lower = error_str.lower()
        return not any(kw in error_lower for kw in non_retryable_keywords)

    def _is_tool_missing_error(self, error_str: str) -> bool:
        """判断错误是否为工具缺失。

        Args:
            error_str: 错误信息字符串

        Returns:
            是否为工具缺失错误
        """
        error_lower = error_str.lower()
        return any(kw in error_lower for kw in self._TOOL_MISSING_KEYWORDS)

    def _is_knowledge_insufficient(self, ctx: PluginContext) -> bool:
        """根据上下文判断是否为知识不足。

        条件：记忆检索结果为空且知识注入也为空，
        说明系统缺少回答当前问题的相关知识。

        Args:
            ctx: 插件执行上下文

        Returns:
            是否为知识不足
        """
        memory_context = ctx.state.get("memory.retrieved", [])
        knowledge_context = ctx.state.get("knowledge.context", "")
        # 记忆为空且知识为空 → 知识不足
        memory_empty = not memory_context or len(memory_context) == 0
        knowledge_empty = not knowledge_context or knowledge_context.strip() == ""
        return memory_empty and knowledge_empty

    def _is_knowledge_insufficient_response(self, result: str) -> bool:
        """检查 LLM 回复中是否包含知识不足的指示。

        Args:
            result: LLM 回复内容

        Returns:
            是否包含知识不足指示
        """
        if not isinstance(result, str):
            return False
        result_lower = result.strip().lower()
        return any(kw in result_lower for kw in self._KNOWLEDGE_INSUFFICIENT_KEYWORDS)

    def _is_strategy_error(self, ctx: PluginContext) -> bool:
        """根据上下文判断是否为策略错误。

        条件：
        1. 连续相同类型的错误次数 >= 3，说明当前策略无效
        2. 总重试次数 >= 5，说明整体策略需要调整

        仅检查连续相同类型失败可避免不同类型重试的
        误判（如一次空响应 + 一次格式错误不应触发策略错误）。

        Args:
            ctx: 插件执行上下文

        Returns:
            是否为策略错误
        """
        retry_count = ctx.state.get("retry.count", 0)
        # 总重试次数上限（兜底保护）
        if retry_count >= 5:
            return True
        # 连续相同类型错误检测
        consecutive_same = ctx.state.get(
            "error_check.consecutive_same_type",
            0,
        )
        return consecutive_same >= 3

    def _handle_knowledge_insufficient(self, ctx: PluginContext) -> dict[str, Any]:
        """处理知识不足。

        memory.retrieved / knowledge.context 是否为空是确定性状态（由配置和
        记忆库内容决定），重试时输入阶段拿到的还是同样的空值，根因不会自愈。
        因此标记为不可重试，直接 failed —— 与 strategy_error 的"不可重试"语义一致，
        避免无效重试空耗配额（历史：子任务曾因 retryable=True 反复重试 6 次仍失败）。

        Args:
            ctx: 插件执行上下文

        Returns:
            错误分析结果字典
        """
        retry_count = ctx.state.get("retry.count", 0)
        category = "knowledge_insufficient"
        consecutive = self._track_consecutive_error(ctx, category)
        analysis = {
            "retryable": False,
            "reason": ("Knowledge insufficient: no memory or knowledge context available"),
            "category": category,
            "retry_count": retry_count,
        }
        return {
            StateKeys.EXECUTION_STATUS: "failed",
            StateKeys.ERROR_ANALYSIS: analysis,
            "error_check.last_error_type": category,
            "error_check.consecutive_same_type": consecutive,
            "__route_signal__": RouteSignal(
                route_type="end",
                reason="Knowledge insufficient (not retryable: memory/knowledge absence is deterministic)",
            ),
        }

    def _handle_strategy_error(self, ctx: PluginContext) -> dict[str, Any]:
        """处理策略错误。

        策略错误标记为不可重试，因为继续用相同策略只会重复失败。

        Args:
            ctx: 插件执行上下文

        Returns:
            错误分析结果字典
        """
        retry_count = ctx.state.get("retry.count", 0)
        category = "strategy_error"
        consecutive = self._track_consecutive_error(ctx, category)
        analysis = {
            "retryable": False,
            "reason": ("Strategy error: repeated failures suggest approach needs change"),
            "category": category,
            "retry_count": retry_count,
        }

        return {
            StateKeys.EXECUTION_STATUS: "failed",
            StateKeys.ERROR_ANALYSIS: analysis,
            "error_check.last_error_type": category,
            "error_check.consecutive_same_type": consecutive,
            "__route_signal__": RouteSignal(
                route_type="end",
                reason=("Strategy error: approach needs change, not retry"),
            ),
        }
