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


class LLMKeyUnresolvedError(Exception):
    """模型调用的 api_key 是未解析的 ``${VAR}`` 占位符。

    占位符（进程环境与项目根 .env 双源均无值）发往上游只会得到以字面量
    ``${VAR}`` 为 key 的鉴权 401，无法排查——发起 HTTP 前抛本错误（fail-closed），
    携带 model/provider/占位符供定位与配置补全。
    """

    def __init__(self, model: str, provider: str, placeholder: str) -> None:
        self.model = model
        self.provider = provider
        self.placeholder = placeholder
        super().__init__(
            f"模型 {model}（provider={provider or '未知'}）的 API key 未配置："
            f"占位符 {placeholder} 在进程环境与项目根 .env 中均未解析。"
            f"请配置该变量后重试（改 .env 需重启内核或触发 sidecar 重载生效）。"
        )
