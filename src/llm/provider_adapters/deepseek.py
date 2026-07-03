"""DeepSeek 适配器。

DeepSeek thinking 模式（thinking.type=enabled）的强制要求：
    messages 中只要有 assistant(tool_calls)，对应的 reasoning_content
    字段必须存在（否则 400）。但内容可以为空字符串。

优化策略：
    采样保留 reasoning_content，避免历史轮次的思考内容累积撑爆上下文。
    默认每 3 轮保留 1 轮完整 rc，其他清空为 ""。
    可在 llm.yaml 的 default_params 中配置：

    deepseek-v4-pro:
      default_params:
        reasoning_retention:
          sample_interval: 3   # 0=全部清空, 1=全量保留, 3=每3轮保留1轮
"""

from __future__ import annotations

from typing import Any

from .base import ProviderAdapter

_DEFAULT_INTERVAL = 3


class DeepSeekAdapter(ProviderAdapter):
    """DeepSeek：采样保留 reasoning_content。"""

    def adapt_messages_before_send(  # noqa: D102
        self,
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> list[dict[str, Any]]:
        retention = kwargs.get("reasoning_retention", {})
        interval = (
            retention.get("sample_interval", _DEFAULT_INTERVAL) if isinstance(retention, dict) else _DEFAULT_INTERVAL
        )
        return _apply_sampling(messages, interval)


def _apply_sampling(
    messages: list[dict[str, Any]],
    interval: int,
) -> list[dict[str, Any]]:
    """按间隔采样保留 reasoning_content。

    Returns 新列表，原 messages 不变。
    """
    if interval <= 0:
        return [_empty_rc(m) if _has_tool_calls(m) else m for m in messages]
    if interval == 1:
        return list(messages)

    result: list[dict[str, Any]] = []
    tc_count = 0
    for m in messages:
        if _has_tool_calls(m):
            tc_count += 1
            if tc_count % interval != 1:
                result.append(_empty_rc(m))
                continue
        result.append(m)
    return result


def _has_tool_calls(msg: dict[str, Any]) -> bool:
    return msg.get("role") == "assistant" and msg.get("tool_calls")


def _empty_rc(msg: dict[str, Any]) -> dict[str, Any]:
    """构造新 dict，reasoning_content 清空但保留字段。"""
    return {k: "" if k == "reasoning_content" else v for k, v in msg.items()}
