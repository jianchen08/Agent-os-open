"""DeepSeek 适配器。

复制锚点：源文件 ``plugins/shared/llm/provider_adapters/deepseek.py`` @
c0fe6922fc8cf68cc0c46b6ff1e11c0db021056a（批 2 收口基线）；逐行同源。

DeepSeek thinking 模式（thinking.type=enabled）的强制要求：
    messages 中只要有 assistant(tool_calls)，对应的 reasoning_content
    字段必须存在（否则 400）。但内容可以为空字符串。

内容级采样保留（reasoning_retention.sample_interval）：
    1 = 全量保留，N≥2 = 每 N 个 tool_calls 轮保留第 1 个，0 = 全部清空。
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
    """DeepSeek：采样保留 reasoning_content。

    ``replay_reasoning = True``（2026-10-21 策略化，依据官方文档口径——本仓
    未实机复测，注明依据）：DeepSeek 新版 thinking 模式带 tools 时，assistant
    消息缺失 ``reasoning_content`` 键直接 400（与 R1 时代"建议剥离"相反）。
    本类覆写 ``adapt_messages_before_send`` 做内容级采样保留（防历史思考累积
    撑爆上下文），类属性供策略矩阵/通用路径读取，两处口径必须一致。
    """

    #: 新版 thinking 模式强制回传（缺键 400，依据文档；内容级采样见类 docstring）。
    replay_reasoning: bool = True

    def adapt_messages_before_send(  # noqa: D102
        self,
        messages: list[dict[str, Any]],
        *,
        replay_reasoning: bool | str | None = None,
        **kwargs: object,
    ) -> list[dict[str, Any]]:
        effective = self._effective_replay(replay_reasoning)
        if not effective:
            # 剥离档：思考内容不回传，但**保留 reasoning_content 键**——DeepSeek
            # thinking 模式带 tools 时缺键直接 400（键须在场，内容允许空串）。
            return _blank_reasoning_content(messages)
        retention = kwargs.get("reasoning_retention", {})
        interval = (
            retention.get("sample_interval", _DEFAULT_INTERVAL) if isinstance(retention, dict) else _DEFAULT_INTERVAL
        )
        return _apply_sampling(messages, interval)


def _blank_reasoning_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """清空 rc 内容、保留 rc 键（DeepSeek thinking 模式 400 防线）。

    Returns 新列表，原 messages 不变。
    """
    result: list[dict[str, Any]] = []
    for m in messages:
        new = dict(m)
        if "reasoning_content" in new or _has_tool_calls(m):
            new["reasoning_content"] = ""
        result.append(new)
    return result


def _apply_sampling(
    messages: list[dict[str, Any]],
    interval: int,
) -> list[dict[str, Any]]:
    """按间隔采样保留 reasoning_content。

    Returns 新列表，原 messages 不变。
    """
    if interval <= 0:
        return [_empty_rc(m) if _has_tool_calls(m) else _ensure_rc(m) for m in messages]
    if interval == 1:
        return [_ensure_rc(m) for m in messages]

    result: list[dict[str, Any]] = []
    tc_count = 0
    for m in messages:
        if _has_tool_calls(m):
            tc_count += 1
            if tc_count % interval != 1:
                result.append(_empty_rc(m))
                continue
        result.append(_ensure_rc(m))
    return result


def _has_tool_calls(msg: dict[str, Any]) -> bool:
    # bool() 显式收敛 and 短路返回的 truthy 值（调用方均为布尔上下文）
    return bool(msg.get("role") == "assistant" and msg.get("tool_calls"))


def _ensure_rc(msg: dict[str, Any]) -> dict[str, Any]:
    """确保 assistant 消息带 reasoning_content 键（缺失补空串）。

    DeepSeek thinking 模式强制要求 messages 中的 assistant 消息必须回传
    reasoning_content（内容可为空串）。历史恢复/压缩重建的消息可能缺失
    该键，直接透传会 400（"The `reasoning_content` in the thinking mode
    must be passed back to the API"）。
    """
    if msg.get("role") != "assistant" or "reasoning_content" in msg:
        return msg
    new = dict(msg)
    new["reasoning_content"] = ""
    return new


def _empty_rc(msg: dict[str, Any]) -> dict[str, Any]:
    """构造新 dict，reasoning_content 清空（缺失补键，字段必须存在）。"""
    new = dict(msg)
    new["reasoning_content"] = ""
    return new
