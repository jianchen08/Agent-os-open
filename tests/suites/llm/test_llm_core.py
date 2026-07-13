"""LLMCore 单元测试。

使用 Mock 替代 LiteLLM 真实调用，验证：
- 基本调用逻辑（raw_result / raw_error / raw_tool_calls）
- 重试机制（超时→重试→成功 / 连续超时→raw_error）
- 工具调用解析
- 流式回调处理
- ToolCore 骨架行为
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import ErrorPolicy, StateKeys, create_initial_state
from plugins.core.llm_core import LLMCore
from plugins.core.tool_core import ToolCore


# ---------------------------------------------------------------------------
# Mock 辅助
# ---------------------------------------------------------------------------


def _make_mock_response(
    content: str | None = "Hello!",
    tool_calls: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """构造 Mock 的 LiteLLM 非流式响应。

    Args:
        content: 响应文本内容
        tool_calls: 工具调用列表（Mock 格式）

    Returns:
        模拟 litellm.acompletion 返回值的 MagicMock
    """
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_message = MagicMock()
    mock_message.content = content
    mock_message.reasoning_content = None

    if tool_calls:
        mock_tc_list = []
        for tc in tool_calls:
            mock_tc = MagicMock()
            mock_tc.function.name = tc["name"]
            mock_tc.function.arguments = tc["arguments"]
            mock_tc_list.append(mock_tc)
        mock_message.tool_calls = mock_tc_list
    else:
        mock_message.tool_calls = None

    mock_choice.message = mock_message
    mock_response.choices = [mock_choice]
    return mock_response


def _make_mock_streaming_response(
    chunks: list[dict[str, Any]],
) -> Any:
    """构造 Mock 的 LiteLLM 流式响应。

    返回一个异步生成器函数，调用后产生模拟的流式 chunks。
    litellm.acompletion(stream=True) 返回异步迭代器，
    使用 async for 遍历。

    Args:
        chunks: 流式片段列表，每个片段包含 type 和对应数据

    Returns:
        异步生成器函数（调用后返回异步迭代器）
    """
    mock_chunks = []
    for chunk_data in chunks:
        mock_chunk = MagicMock()
        mock_delta = MagicMock()
        mock_choice = MagicMock()

        mock_delta.reasoning_content = None

        if chunk_data.get("type") == "text":
            mock_delta.content = chunk_data["content"]
            mock_delta.tool_calls = None
        elif chunk_data.get("type") == "tool_call":
            mock_delta.content = None
            mock_tc_list = []
            for tc in chunk_data.get("tool_calls", []):
                mock_tc = MagicMock()
                mock_tc.index = tc.get("index", 0)
                mock_tc.function = MagicMock()
                mock_tc.function.name = tc.get("name", "")
                mock_tc.function.arguments = tc.get("arguments", "")
                mock_tc_list.append(mock_tc)
            mock_delta.tool_calls = mock_tc_list
        else:
            mock_delta.content = None
            mock_delta.tool_calls = None

        mock_choice.delta = mock_delta
        mock_chunk.choices = [mock_choice]
        mock_chunks.append(mock_chunk)

    async def _stream_generator():
        for chunk in mock_chunks:
            yield chunk

    return _stream_generator


# ---------------------------------------------------------------------------
# LLMCore 测试
# ---------------------------------------------------------------------------


class TestLLMCore:
    """LLMCore 单元测试。"""

    def test_llm_core_properties(self) -> None:
        """验证 LLMCore 基本属性。"""
        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        assert core.name == "llm_core"
        assert core.priority == 50
        assert core.error_policy == ErrorPolicy.RETRY
        assert core.max_retries == 3
        assert core.retry_delay == 1.0

    def test_llm_core_config_override(self) -> None:
        """验证配置可覆盖类属性。"""
        core = LLMCore(config={
            "provider": "minimax",
            "model_name": "MiniMax-M2.7",
            "max_retries": 5,
            "retry_delay": 2.0,
        })
        assert core.max_retries == 5
        assert core.retry_delay == 2.0
        assert core._provider == "minimax"
        assert core._model == "MiniMax-M2.7"

    @patch("llm.adapter.litellm")
    async def test_llm_core_basic_call(self, mock_litellm: MagicMock) -> None:
        """Mock LiteLLM，验证 raw_result 正确写入。

        流程：
          state 包含 messages -> _call_completion 返回 Mock 响应
          -> raw_result = "Hello!", raw_error = None, raw_tool_calls = []
        """
        mock_litellm.acompletion = AsyncMock(
            return_value=_make_mock_response(content="Hello!")
        )
        # 确保 Timeout 等异常类存在（使用类名匹配的异常类）
        class Timeout(Exception):
            pass
        mock_litellm.Timeout = Timeout
        mock_litellm.ServiceUnavailableError = type("ServiceUnavailableError", (Exception,), {})
        mock_litellm.RateLimitError = type("RateLimitError", (Exception,), {})
        mock_litellm.APIConnectionError = type("APIConnectionError", (Exception,), {})

        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        state = create_initial_state(
            messages=[{"role": "user", "content": "Hi"}]
        )
        ctx = PluginContext(state=state, config={})
        result = await core.execute(ctx)

        assert result[StateKeys.RAW_RESULT] == "Hello!"
        assert result[StateKeys.RAW_ERROR] is None
        assert result[StateKeys.RAW_TOOL_CALLS] == []

    @patch("llm.adapter.litellm")
    async def test_llm_core_retry_on_timeout(self, mock_litellm: MagicMock) -> None:
        """Mock 超时 → 重试 → 成功。

        LLMCore.execute() 不内置重试逻辑，重试由 PluginChain 的 error_policy
        统一管理。此处模拟 PluginChain 的重试行为：捕获异常后再次调用 execute，
        第二次调用成功返回结果。

        流程：
          第一次调用抛出 Timeout → 捕获异常
          第二次调用成功返回 "Retry OK"
          验证 raw_result = "Retry OK", raw_error = None
        """
        # 创建名为 Timeout 的异常类，使 _is_retryable_error 通过类名匹配
        class Timeout(Exception):
            pass

        # 先超时，后成功
        mock_litellm.acompletion = AsyncMock(
            side_effect=[
                Timeout("Connection timed out"),
                _make_mock_response(content="Retry OK"),
            ]
        )
        mock_litellm.Timeout = Timeout
        mock_litellm.ServiceUnavailableError = Exception
        mock_litellm.RateLimitError = Exception
        mock_litellm.APIConnectionError = Exception

        core = LLMCore(config={
            "provider": "openai",
            "model_name": "gpt-4",
            "max_retries": 3,
            "retry_delay": 0.01,  # 测试时缩短延迟
        })
        state = create_initial_state(
            messages=[{"role": "user", "content": "Hi"}]
        )
        ctx = PluginContext(state=state, config={})

        # 模拟 PluginChain 的重试逻辑：第一次超时，第二次成功
        with pytest.raises(Timeout):
            await core.execute(ctx)

        # 第二次调用（重试）成功
        result = await core.execute(ctx)
        assert result[StateKeys.RAW_RESULT] == "Retry OK"
        assert result[StateKeys.RAW_ERROR] is None

    @patch("llm.adapter.litellm")
    async def test_llm_core_retry_exhausted(self, mock_litellm: MagicMock) -> None:
        """Mock 连续超时 → 异常抛出。

        LLMCore.execute() 不内置重试逻辑，失败时直接抛出异常。
        重试耗尽后，最终异常由调用方（PluginChain）捕获并写入 raw_error。
        此处验证：多次调用均抛出 Timeout 异常。

        流程：
          所有调用都超时 → 重试耗尽
          验证每次调用均抛出 Timeout 异常
        """
        class Timeout(Exception):
            pass

        mock_litellm.acompletion = AsyncMock(
            side_effect=Timeout("Connection timed out")
        )
        mock_litellm.Timeout = Timeout
        mock_litellm.ServiceUnavailableError = Exception
        mock_litellm.RateLimitError = Exception
        mock_litellm.APIConnectionError = Exception

        core = LLMCore(config={
            "provider": "openai",
            "model_name": "gpt-4",
            "max_retries": 2,
            "retry_delay": 0.01,
        })
        state = create_initial_state(
            messages=[{"role": "user", "content": "Hi"}]
        )
        ctx = PluginContext(state=state, config={})

        # LLMCore.execute() 失败时直接抛出异常，不做重试
        # 模拟 PluginChain 重试耗尽：连续 max_retries + 1 次调用均抛出异常
        for _ in range(core.max_retries + 1):
            with pytest.raises(Timeout, match="Connection timed out"):
                await core.execute(ctx)

    @patch("llm.adapter.litellm")
    async def test_llm_core_tool_calls(self, mock_litellm: MagicMock) -> None:
        """Mock LLM 返回 tool_calls → raw_tool_calls 正确解析。

        流程：
          LLM 返回包含 tool_calls 的响应
          验证 raw_tool_calls 包含正确的 name 和 arguments 字段
          _parse_tool_calls 返回的字典包含 id、name、arguments 三个字段
        """
        mock_response = _make_mock_response(
            content=None,
            tool_calls=[
                {"name": "read_file", "arguments": '{"path": "/tmp/test.txt"}'},
                {"name": "search", "arguments": '{"query": "hello"}'},
            ],
        )
        # 为 mock tool_call 对象设置显式 id（MagicMock 自动属性会返回 MagicMock 对象）
        mock_response.choices[0].message.tool_calls[0].id = "call_0"
        mock_response.choices[0].message.tool_calls[1].id = "call_1"

        mock_litellm.acompletion = AsyncMock(return_value=mock_response)
        class Timeout(Exception):
            pass
        mock_litellm.Timeout = Timeout
        mock_litellm.ServiceUnavailableError = type("ServiceUnavailableError", (Exception,), {})
        mock_litellm.RateLimitError = type("RateLimitError", (Exception,), {})
        mock_litellm.APIConnectionError = type("APIConnectionError", (Exception,), {})

        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        state = create_initial_state(
            messages=[{"role": "user", "content": "Read the file"}]
        )
        ctx = PluginContext(state=state, config={})
        result = await core.execute(ctx)

        # _parse_tool_calls 返回 {"id": ..., "name": ..., "arguments": ...} 格式
        tool_calls = result[StateKeys.RAW_TOOL_CALLS]
        assert len(tool_calls) == 2
        assert tool_calls[0]["id"] == "call_0"
        assert tool_calls[0]["name"] == "read_file"
        assert tool_calls[0]["arguments"] == '{"path": "/tmp/test.txt"}'
        assert tool_calls[1]["id"] == "call_1"
        assert tool_calls[1]["name"] == "search"
        assert tool_calls[1]["arguments"] == '{"query": "hello"}'

    @patch("llm.adapter.litellm")
    async def test_llm_core_streaming(self, mock_litellm: MagicMock) -> None:
        """Mock 流式回调 → 验证 chunk 正确处理。

        流程：
          state["streaming"] = True
          state["on_chunk"] = 回调函数
          LLM 返回流式 chunks
          验证：回调被正确触发，raw_result 包含完整文本
        """
        chunks_data = [
            {"type": "text", "content": "Hello"},
            {"type": "text", "content": " World"},
            {"type": "text", "content": "!"},
        ]
        stream_gen = _make_mock_streaming_response(chunks_data)
        mock_litellm.acompletion = AsyncMock(return_value=stream_gen())
        class Timeout(Exception):
            pass
        mock_litellm.Timeout = Timeout
        mock_litellm.ServiceUnavailableError = type("ServiceUnavailableError", (Exception,), {})
        mock_litellm.RateLimitError = type("RateLimitError", (Exception,), {})
        mock_litellm.APIConnectionError = type("APIConnectionError", (Exception,), {})

        received_chunks: list[dict[str, Any]] = []

        def on_chunk(chunk: dict[str, Any]) -> None:
            received_chunks.append(chunk)

        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        state = create_initial_state(
            messages=[{"role": "user", "content": "Hi"}],
            streaming=True,
            on_chunk=on_chunk,
        )
        ctx = PluginContext(state=state, config={})
        result = await core.execute(ctx)

        assert result[StateKeys.RAW_RESULT] == "Hello World!"
        assert len(received_chunks) == 3
        assert all(c["type"] == "text" for c in received_chunks)

    @patch("llm.adapter.litellm")
    async def test_llm_core_streaming_with_tool_calls(
        self, mock_litellm: MagicMock
    ) -> None:
        """Mock 流式回调 + 工具调用 → 验证 tool_calls 正确拼接。

        流程：
          流式返回文本和工具调用增量
          验证 raw_tool_calls 正确组装
        """
        chunks_data = [
            {"type": "text", "content": "Let me help"},
            {
                "type": "tool_call",
                "tool_calls": [
                    {"index": 0, "name": "read_file", "arguments": '{"path":'},
                ],
            },
            {
                "type": "tool_call",
                "tool_calls": [
                    {"index": 0, "name": "", "arguments": ' "/tmp/a.txt"}'},
                ],
            },
        ]
        stream_gen = _make_mock_streaming_response(chunks_data)
        mock_litellm.acompletion = AsyncMock(return_value=stream_gen())
        class Timeout(Exception):
            pass
        mock_litellm.Timeout = Timeout
        mock_litellm.ServiceUnavailableError = type("ServiceUnavailableError", (Exception,), {})
        mock_litellm.RateLimitError = type("RateLimitError", (Exception,), {})
        mock_litellm.APIConnectionError = type("APIConnectionError", (Exception,), {})

        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        state = create_initial_state(
            messages=[{"role": "user", "content": "Read file"}],
            streaming=True,
        )
        ctx = PluginContext(state=state, config={})
        result = await core.execute(ctx)

        assert result[StateKeys.RAW_RESULT] == "Let me help"
        assert len(result[StateKeys.RAW_TOOL_CALLS]) == 1
        assert result[StateKeys.RAW_TOOL_CALLS][0]["name"] == "read_file"
        assert result[StateKeys.RAW_TOOL_CALLS][0]["arguments"] == '{"path": "/tmp/a.txt"}'

    async def test_llm_core_fallback_user_input(self) -> None:
        """验证 _build_messages 从 messages 字段构建消息列表。

        _build_messages 从 state["messages"] 读取对话历史，
        不直接处理 user_input。user_input 由上游输入插件转换为
        messages 格式后传入。
        """
        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        state = create_initial_state(
            messages=[{"role": "user", "content": "Hello from user"}]
        )

        messages = core._build_messages(state)
        assert messages == [{"role": "user", "content": "Hello from user"}]

    async def test_llm_core_empty_state(self) -> None:
        """当 state 中既没有 messages 也没有 user_input 时。"""
        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        state = create_initial_state()
        # 移除默认的 messages（如果有）
        state.pop("messages", None)

        messages = core._build_messages(state)
        assert messages == []

    def test_llm_core_get_model_string(self) -> None:
        """验证 LiteLLM 模型字符串格式。"""
        core_openai = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        assert core_openai._get_model_string() == "openai/gpt-4"

        core_minimax = LLMCore(config={"provider": "minimax", "model_name": "MiniMax-M2.7"})
        assert core_minimax._get_model_string() == "minimax/MiniMax-M2.7"

        core_custom = LLMCore(config={"provider": "custom_provider", "model_name": "my-model"})
        assert core_custom._get_model_string() == "custom_provider/my-model"


# ---------------------------------------------------------------------------
# ToolCore 测试
# ---------------------------------------------------------------------------


class TestToolCore:
    """ToolCore 骨架测试。"""

    def test_tool_core_properties(self) -> None:
        """验证 ToolCore 基本属性。"""
        core = ToolCore()
        assert core.name == "tool_core"
        assert core.priority == 50
        assert core.error_policy == ErrorPolicy.RETRY

    async def test_tool_core_no_tool_calls(self) -> None:
        """当没有工具调用时返回提示。"""
        core = ToolCore()
        state = create_initial_state()
        ctx = PluginContext(state=state, config={})
        result = await core.execute(ctx)

        assert result[StateKeys.RAW_RESULT] == "No tool calls to execute"
        assert result[StateKeys.RAW_ERROR] is None

    async def test_tool_core_with_tool_calls(self) -> None:
        """当有工具调用但工具未注册时返回错误。"""
        core = ToolCore()
        state = create_initial_state(
            **{StateKeys.RAW_TOOL_CALLS: [
                {"name": "read_file", "arguments": '{"path": "/tmp/test.txt"}'},
            ]}
        )
        ctx = PluginContext(state=state, config={})
        result = await core.execute(ctx)

        # M3: ToolCore 实际尝试执行工具，未注册的工具返回 not found 错误
        tool_results = result[StateKeys.TOOL_RESULTS]
        assert len(tool_results) == 1
        assert tool_results[0]["success"] is False
        assert "not found" in tool_results[0]["error"]
        assert tool_results[0]["tool_name"] == "read_file"

    def test_tool_core_register_tool(self) -> None:
        """验证工具注册功能。"""
        core = ToolCore()

        def my_tool(x: int) -> int:
            return x * 2

        core.register_tool("my_tool", my_tool)
        assert "my_tool" in core._tools
        assert core._tools["my_tool"](5) == 10
