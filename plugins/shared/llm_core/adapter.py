"""LLM 调用面类型与协议归属地。

llm_core 的 LLM 调用唯一事实源 = llm_service（llm.complete_stream，经内核
tool-executor 能力跨进程调用）；直连 litellm 的适配器实现已随调用面统一
退役（2026-09-06 T7，见 ADR 2026-09-06-llm-normalize-single-point）。
本模块保留一份契约：

- ``LLMResponse``：聚合响应结构（与 llm.complete_stream 返回 dict 同构，
  成功路径由 ``_call_llm`` 组装）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMResponse:
    """统一 LLM 响应结构。

    Attributes:
        text: LLM 响应文本内容
        tool_calls: 解析后的工具调用列表
        thinking_text: 思考过程文本（如 DeepSeek reasoning_content）
        usage: token 用量信息
        finish_reason: LLM 返回的结束原因（stop/length/tool_calls…）。
            ``length`` 表示因命中 max_tokens 被截断，此时 tool_call 的
            arguments JSON 可能不完整，下游需据此识别并处理截断。
        stream_repetition: 流式退化循环守卫判定并截断（输出原地重复）。
            True 表示本次输出被主动掐断，``finish_reason`` 收敛为 length。
        thinking_truncated: 思考内容因超过 max_thinking_chars 被截断
            （适配服务面流式消费产物；网关聚合同构字段）。
    """

    text: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    thinking_text: str | None = None
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    stream_repetition: bool = False
    thinking_truncated: bool = False
