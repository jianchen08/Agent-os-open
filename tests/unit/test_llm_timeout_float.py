"""timeout 必须为 float 的回归测试。

回归核心问题：zai provider 严格 isinstance(timeout, float) 校验，传入 int 会触发
    BadRequestError "Timeout needs to be a float or httpx.Timeout"。
两个发源点：
1. yaml defaults.call_timeout 是 int → Router 的 timeout/stream_timeout 是 int
2. 非流式路径不显式设 timeout → 沿用 litellm/Router 的 int 默认

修复后两处都必须保证 float，否则同一个 BadRequestError 又会被 error_classifier
归成 bad_request 漏进对话历史。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from llm.adapter import LiteLLMAdapter


class _Msg:
    def __init__(self) -> None:
        self.content = "ok"
        self.tool_calls = None
        self.reasoning_content = None


class _Choice:
    def __init__(self) -> None:
        self.message = _Msg()


class _FakeResponse:
    """最小可解析的非流式响应（_call_non_streaming 只读 choices[0].message）。"""

    def __init__(self) -> None:
        self.choices = [_Choice()]
        self.usage = None


class _MockLoader:
    """build_router 依赖的 model_loader 替身。"""

    def __init__(self, data: dict) -> None:
        self._data = data

    def _load_llm_data(self) -> dict:
        return self._data


class TestNonStreamingTimeoutIsFloat:
    """非流式路径传给 litellm 的 timeout 必须是 float。"""

    @pytest.mark.asyncio
    async def test_int_inter_chunk_timeout_becomes_float(self) -> None:
        """plugin 传入 int inter_chunk_timeout，adapter 必须转 float 设为 timeout。"""
        captured: dict = {}

        async def _fake_acompletion(**kwargs: object) -> _FakeResponse:
            captured.update(kwargs)
            return _FakeResponse()

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr("llm.adapter.litellm.acompletion", _fake_acompletion)
            adapter = LiteLLMAdapter()
            await adapter.completion(
                model="zai/glm-5.2",
                messages=[{"role": "user", "content": "hi"}],
                stream=False,
                inter_chunk_timeout=120,  # 模拟 plugin 传的 int call_timeout
                first_chunk_timeout=60,
            )

        assert "timeout" in captured
        assert isinstance(captured["timeout"], float)
        assert captured["timeout"] == 120.0
        # 流式专属参数不得透传给 litellm
        assert "inter_chunk_timeout" not in captured
        assert "first_chunk_timeout" not in captured

    @pytest.mark.asyncio
    async def test_no_inter_chunk_timeout_still_float(self) -> None:
        """未传 inter_chunk_timeout 时也必须有 float timeout（默认 300）。"""
        captured: dict = {}

        async def _fake_acompletion(**kwargs: object) -> _FakeResponse:
            captured.update(kwargs)
            return _FakeResponse()

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr("llm.adapter.litellm.acompletion", _fake_acompletion)
            adapter = LiteLLMAdapter()
            await adapter.completion(
                model="zai/glm-5.2",
                messages=[{"role": "user", "content": "hi"}],
                stream=False,
            )

        assert isinstance(captured["timeout"], float)
        assert captured["timeout"] == 300.0


class TestRouterTimeoutIsFloat:
    """Router 构建的 timeout/stream_timeout 必须是 float。"""

    def test_int_yaml_call_timeout_becomes_float(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """yaml call_timeout 为 int（120），Router 必须收到 float。"""
        from llm import router_factory

        monkeypatch.setattr(router_factory, "disable_llm_proxy", lambda: None)
        captured: dict = {}

        def _fake_router(**kwargs: object) -> MagicMock:
            captured.update(kwargs)
            return MagicMock()

        monkeypatch.setattr(router_factory.litellm, "Router", _fake_router)

        loader = _MockLoader(
            {"defaults": {"call_timeout": 120}, "providers": {}, "models": {}},
        )
        try:
            router_factory.build_router(loader)
        finally:
            router_factory.reset_router()

        assert isinstance(captured["timeout"], float)
        assert captured["timeout"] == 120.0
        assert isinstance(captured["stream_timeout"], float)
        assert captured["stream_timeout"] == 120.0
