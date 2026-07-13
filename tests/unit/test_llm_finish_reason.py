"""LLMResponse.finish_reason 捕获单元测试。

回归契约：adapter 必须把 LLM 返回的 finish_reason 透传到 LLMResponse，
llm_core 据此判断 output_truncated（finish_reason=="length" 即被 max_tokens 截断）。
"""

from __future__ import annotations

from llm.adapter import LLMResponse


class TestLLMResponseFinishReason:
    """LLMResponse 新增 finish_reason 字段，默认 None。"""

    def test_finish_reason_default_none(self) -> None:
        resp = LLMResponse()
        assert resp.finish_reason is None

    def test_finish_reason_set(self) -> None:
        resp = LLMResponse(finish_reason="length")
        assert resp.finish_reason == "length"

    def test_finish_reason_stop(self) -> None:
        resp = LLMResponse(finish_reason="stop")
        assert resp.finish_reason == "stop"


class TestOutputTruncatedDerivation:
    """output_truncated = (finish_reason == "length") 的派生逻辑（与 llm_core 一致）。"""

    def test_length_means_truncated(self) -> None:
        resp = LLMResponse(finish_reason="length")
        assert (resp.finish_reason == "length") is True

    def test_stop_means_not_truncated(self) -> None:
        resp = LLMResponse(finish_reason="stop")
        assert (resp.finish_reason == "length") is False

    def test_none_means_not_truncated(self) -> None:
        resp = LLMResponse()
        assert (resp.finish_reason == "length") is False
