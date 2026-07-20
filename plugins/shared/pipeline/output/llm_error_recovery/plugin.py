"""LLM 错误恢复 Output 插件。

负责在管道循环的输出阶段读取 state["llm_error_info"]，
为 LLM 可修复错误（仅 llm_fixable）构建恢复提示并追加到 messages。
infrastructure_error / context_overflow / unknown 类型不追加提示
（LLM 无法通过调整操作修复，应由 error_check 路由决策接管）。

从 engine.py 中迁移的逻辑：
- _build_llm_error_hint：根据错误类型生成面向大模型的恢复建议
- LLM 可修复错误的恢复提示构建与 messages 追加

State 命名空间：
    - llm_error_info : engine.py 在 core 异常时写入的错误信息
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import ErrorPolicy

logger = logging.getLogger(__name__)

# bad_request 中 LLM 确实能通过调整操作修复的关键词（白名单）。
# 命中其一才追加恢复提示，其余 bad_request 一律不喂给 LLM（默认安全）。
_FIXABLE_BAD_REQUEST_KEYWORDS = (
    "invalid function arguments",
    "invalid params",
)


def _is_bad_request_fixable(error_msg: str) -> bool:
    """判断 bad_request 错误是否 LLM 真能通过调整操作修复。

    白名单默认安全：只有工具参数 JSON 格式错（invalid function arguments /
    invalid params）或工具调用序列破坏（tool id ... not found）这类 LLM 改
    参数就能修的才返回 True。其余 bad_request（如 timeout 类型错
    "Timeout needs to be a float"、1000 条输入限制、context_length_exceeded、
    max_tokens 超限）LLM 改参数也修不了，返回 False，避免把错误文本塞进
    对话历史污染上下文。

    Args:
        error_msg: 原始错误信息字符串

    Returns:
        是否可修复（True 才追加恢复提示到 messages）
    """
    lower = error_msg.lower()
    if any(kw in lower for kw in _FIXABLE_BAD_REQUEST_KEYWORDS):
        return True
    # tool id not found：消息序列被破坏（工具调用与结果失配），LLM 重发调用可修
    if "tool id" in lower and "not found" in lower:
        return True
    return False


class LLMErrorRecoveryPlugin(IOutputPlugin):
    """LLM 错误恢复 Output 插件。

    读取 ctx.state["llm_error_info"]，根据错误类型构建恢复提示：
    - context_overflow：不追加提示，由压缩插件处理
    - infrastructure_error：不追加提示，LLM 无法修复（认证/权限/连接/key 耗尽），
      由 error_check 插件的路由决策接管（重试耗尽产出 end，drain 发 stream_error）
    - llm_fixable：构建错误提示和恢复建议，追加到 messages
    - unknown：构建通用错误提示，追加到 messages

    优先级：3（在 error_check[priority=2] 之后执行）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.ABORT

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化 LLM 错误恢复插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用（默认 True）
        """
        self._config = config or {}

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "llm_error_recovery"

    @property
    def priority(self) -> int:
        """插件执行优先级。

        在 error_check（priority=2）之后执行。
        """
        return self._config.get("priority", 3)

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """执行 LLM 错误恢复逻辑。

        读取 state["llm_error_info"]，如果有错误信息，
        根据错误类型构建恢复提示并追加到 messages。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含 messages 更新的输出结果
        """
        error_info = ctx.state.get("llm_error_info")
        if not error_info:
            # 没有错误信息，不做任何处理
            return OutputResult()

        error_type = error_info.get("error_type", "unknown")
        error_msg = error_info.get("error_msg", "")

        # context_overflow 类型不追加提示，由压缩插件处理
        if error_type == "context_overflow":
            logger.info(
                "LLM error recovery: context_overflow detected, "
                "skipping hint (handled by compression plugin) | error=%s",
                error_msg[:200],
            )
            # 清除错误信息，避免重复处理
            return OutputResult(state_updates={"llm_error_info": None})

        # bad_request：LLM 有能力修复（如非法工具参数），构建恢复提示。
        # 其余（rate_limit/quota_exhausted/auth_failed/service_down/network/
        # server_error/unknown）属于基础设施错误，LLM 无法通过调整操作修复，
        # 追加面向 LLM 的提示无意义。直接清除，由 error_check 路由决策接管。
        if error_type != "bad_request":
            logger.warning(
                "LLM error recovery: %s detected, skipping hint (LLM cannot fix this) | error=%s",
                error_type,
                error_msg[:200],
            )
            return OutputResult(state_updates={"llm_error_info": None})

        # 白名单默认安全：bad_request 还要再判定"LLM 是否真能修"。
        # 只有工具参数 JSON 错 / tool id 序列破坏这类改参数就能修的才喂；
        # 其余 bad_request（如 "Timeout needs to be a float"、1000 条输入限制、
        # context_length_exceeded、max_tokens 超限）是 API 层面错误，LLM 改参数
        # 也修不了，喂进去只会污染对话历史。不喂，清除错误信息由 error_check 接管。
        if not _is_bad_request_fixable(error_msg):
            logger.info(
                "LLM error recovery: bad_request 非可修复类型，跳过提示（不喂 LLM） | error=%s",
                error_msg[:200],
            )
            return OutputResult(state_updates={"llm_error_info": None})

        # 可修复的 bad_request：构建恢复提示
        hint = self._build_llm_error_hint(error_msg)
        messages = list(ctx.state.get("messages", []))

        # 确保不破坏 provider 要求的消息序列：
        # - assistant(tool_calls) 后必须紧跟 tool 消息
        # - 不能在 assistant(tool_calls) 和 tool 之间插入 user 消息
        # 因此根据最后一条消息的角色决定如何追加错误提示
        if not messages:
            messages.append(
                {
                    "role": "user",
                    "content": (f"[系统错误] 上一次 LLM 调用失败：{error_msg[:300]}\n建议：{hint}"),
                }
            )
        elif messages[-1].get("role") == "tool":
            # 合并到 tool 消息，保持 assistant(tool_calls) → tool* 序列完整
            last_msg = dict(messages[-1])
            original = last_msg.get("content", "")
            last_msg["content"] = f"{original}\n\n[系统提示] 上一次 LLM 调用失败：{error_msg[:300]}\n建议：{hint}"
            messages[-1] = last_msg
        elif messages[-1].get("role") == "assistant" and messages[-1].get("tool_calls"):
            # assistant 带 tool_calls 但还没收到 tool 结果
            # 不能插入 user 消息，合并到 assistant 消息的 content 中
            last_msg = dict(messages[-1])
            original = last_msg.get("content") or ""
            last_msg["content"] = (
                f"{original}\n\n[系统提示] 上一次 LLM 调用失败：{error_msg[:300]}\n建议：{hint}"
                if original
                else (f"[系统提示] 上一次 LLM 调用失败：{error_msg[:300]}\n建议：{hint}")
            )
            messages[-1] = last_msg
        else:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"[系统错误] 上一次 LLM 调用失败，请根据以下提示调整你的操作：\n\n"
                        f"错误信息：{error_msg[:500]}\n\n"
                        f"建议：{hint}"
                    ),
                }
            )

        logger.info(
            "LLM error recovery: appended hint for error_type=%s | error=%s",
            error_type,
            error_msg[:200],
        )

        # 清除错误信息，避免重复处理
        return OutputResult(
            state_updates={
                "messages": messages,
                "llm_error_info": None,
            }
        )

    @staticmethod
    def _build_llm_error_hint(error_msg: str) -> str:  # noqa: PLR0911
        """根据 LLM 错误信息生成给大模型的恢复建议。

        Args:
            error_msg: 原始错误信息字符串

        Returns:
            面向大模型的恢复建议文本
        """
        error_lower = error_msg.lower()
        # MiniMax (2013) tool id not found — 消息序列被破坏，不是参数问题
        if "tool id" in error_lower and "not found" in error_lower:
            return "API 报告工具调用序列异常。请重新发起工具调用，不要继续之前的调用链。"
        if "invalid function arguments" in error_lower or "invalid params" in error_lower:
            return (
                "你上一次的工具调用参数 JSON 格式无效，可能是因为参数内容过长被截断。"
                "请尝试以下方法：\n"
                "1. 如果是 file_write：请将长内容拆分为多次写入，每次只写入一个章节（使用 action='write' 多次调用）\n"
                "2. 如果是其他工具：请减少参数中的文本量，分步操作\n"
                "3. 不要在一次工具调用中传入超过 2000 字符的文本内容"
            )
        if "timeout" in error_lower or "timed out" in error_lower:
            # 区分 stream chunk 超时（服务端无响应）和总调用超时（输出过长）
            is_stream_chunk_timeout = "no data for" in error_lower or "stream chunk timeout" in error_lower
            if is_stream_chunk_timeout:
                return (
                    "API 服务端长时间未返回数据（连接超时）。"
                    "这通常是服务端或网络临时问题，请直接重试当前操作。"
                    "如果持续失败，请稍等片刻后再试。"
                )
            return "上一次 LLM 调用超时。请尝试简化你的请求或缩短输出内容。"
        if "rate limit" in error_lower or "429" in error_lower:
            return "API 调用频率超限，请稍后重试。你可以先输出一段文本回复，下一轮再尝试工具调用。"
        if "context_length" in error_lower or "token limit" in error_lower or "max_tokens" in error_lower:
            return "对话上下文过长，已超出模型限制。请尝试完成当前任务并调用 task_evaluate 结束，或者精简后续操作步骤。"
        return "请检查你的操作是否正确，调整后重试。如果多次失败，请尝试换一种方式完成任务。"
