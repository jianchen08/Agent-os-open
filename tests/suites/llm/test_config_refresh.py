"""验证模型配置实时生效修复。

修复方案：Engine 每次迭代前调用 apply_agent_model_override，
从 YAML 重新解析 model_id，LLMCore 不感知配置来源。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from plugins.core.llm_core import LLMCore
from pipeline.plugin import PluginContext
from pipeline.types import create_initial_state


def _make_config(**overrides) -> dict:
    """构造 LLMCore 配置。"""
    base = {"context_window": 32000, "default_params": {"temperature": 0.7}}
    base.update(overrides)
    return base


def _make_ctx() -> PluginContext:
    """构造 PluginContext。"""
    state = create_initial_state()
    state["messages"] = [{"role": "user", "content": "hi"}]
    return PluginContext(state=state, config={})


def _make_mock_response(content: str = "Hello!") -> MagicMock:
    """构造 Mock LLM 响应。"""
    mock_resp = MagicMock()
    mock_choice = MagicMock()
    mock_msg = MagicMock()
    mock_msg.content = content
    mock_msg.tool_calls = None
    mock_msg.reasoning_content = None
    mock_choice.message = mock_msg
    mock_resp.choices = [mock_choice]
    return mock_resp


class TestLLMCoreIsDumb:
    """LLMCore 不感知配置来源，只管执行。"""

    @pytest.mark.asyncio
    async def test_llm_core_uses_model_id_directly(self):
        """LLMCore 直接使用传入的 model_id，不读 YAML。"""
        config = _make_config(model_name="minimax-m2.7")
        core = LLMCore(config=config)

        mock_adapter = MagicMock()
        mock_adapter.completion = AsyncMock(return_value=_make_mock_response())
        core._adapter = mock_adapter
        core._use_router = False

        ctx = _make_ctx()
        await core.execute(ctx)

        assert core._model == "minimax-m2.7"

    @pytest.mark.asyncio
    async def test_model_id_can_be_overridden_externally(self):
        """Engine 可以在 execute 前直接修改 _model。"""
        config = _make_config(model_name="deepseek-v4-flash")
        core = LLMCore(config=config)

        # Engine 层直接修改 model_id（模拟 apply_agent_model_override）
        core._model = "minimax-m2.7"

        mock_adapter = MagicMock()
        mock_adapter.completion = AsyncMock(return_value=_make_mock_response())
        core._adapter = mock_adapter
        core._use_router = False

        ctx = _make_ctx()
        await core.execute(ctx)

        assert core._model == "minimax-m2.7"
