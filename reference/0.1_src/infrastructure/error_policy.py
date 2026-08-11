"""框架级错误策略处理。

ErrorPolicy 枚举已在 pipeline/types.py 中定义，
本模块提供根据错误策略生成 PluginResult 的框架级函数。

精简原则：
- 保留四种策略（ABORT/SKIP/RETRY/FALLBACK）
- RETRY 的重试逻辑由调用方实现（因为需要重新调用 execute）
- 此函数只处理策略判断和结果生成
"""

from __future__ import annotations

from pipeline.plugin import PluginResult
from pipeline.types import ErrorPolicy


def apply_error_policy(
    policy: ErrorPolicy,
    error: Exception,
    plugin_name: str,
    fallback_state: dict | None = None,
    max_retries: int = 3,
    retry_delay: float = 1.0,
) -> PluginResult:
    """根据错误策略生成 PluginResult。

    注意：RETRY 的重试逻辑由调用方实现（因为需要重新调用 execute），
    此函数只处理重试耗尽后的结果。调用方应在重试耗尽后，
    以 RETRY 策略调用此函数，此时行为等同于 ABORT。

    Args:
        policy: 错误处理策略
        error: 发生的异常
        plugin_name: 插件名称（用于日志）
        fallback_state: FALLBACK 策略使用的默认状态更新
        max_retries: 最大重试次数（仅用于记录，重试由调用方执行）
        retry_delay: 重试间隔秒数（仅用于记录，重试由调用方执行）

    Returns:
        根据策略生成的 PluginResult
    """
    if policy == ErrorPolicy.ABORT:
        return PluginResult(skip_remaining=True, error=error)

    if policy == ErrorPolicy.SKIP:
        return PluginResult(error=error)

    if policy == ErrorPolicy.FALLBACK:
        return PluginResult(state_updates=fallback_state or {}, error=error)

    # RETRY: 重试耗尽后走 ABORT 逻辑
    return PluginResult(skip_remaining=True, error=error)
