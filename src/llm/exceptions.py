"""LLM 领域异常定义。

将 LLM 调用链路中可预期的资源耗尽类错误从 RuntimeError 区分出来，
让调用方能按异常类型精确处理（而非字符串嗅探），符合
backend_rules §3.1「捕获具体异常而非 Exception」。
"""

from __future__ import annotations


class LLMResourceError(Exception):
    """LLM 资源类错误的基类。"""


class KeyPoolExhaustedError(LLMResourceError):
    """所有 key 不可用且等待超时。

    携带诊断信息（不可用 key 列表），让调用方和日志能定位是哪些 key
    在冷却/耗尽，而非只看到「所有 key 不可用」这种无法排查的信息。
    """

    def __init__(self, pool_id: str, timeout: float, unavailable: list[str]) -> None:
        self.pool_id = pool_id
        self.timeout = timeout
        self.unavailable = unavailable
        super().__init__(
            f"KeyPool '{pool_id}' 所有 key 不可用，等待 {timeout:.0f}s 后超时；不可用 key 诊断: {unavailable}"
        )
