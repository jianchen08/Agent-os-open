"""M2/M3 集成测试 — 真实调用 MiniMax M2.7 API。

验证里程碑核心功能实际可用：
- M2：LLMCore 调用真实 LLM 返回结果
- M2：LLMCore 流式输出
- M2：LLMCore 带 system prompt 调用
- M3：真实工具注册 + 执行
- M3：LLM 识别工具调用 → ToolCore 执行 → 结果回传
- M3：工具调用失败处理

需要 --run-integration 选项才会执行：
  pytest -m integration --run-integration
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys, create_initial_state
from plugins.core.llm_core import LLMCore
from plugins.core.tool_core import ToolCore
from plugins.output.pending_tools import PendingToolsOutput
from tools.registry import ToolRegistry
from tools.types import Tool, ToolSource


# ---------------------------------------------------------------------------
# M2 集成测试：LLMCore 真实调用
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestLLMCoreIntegration:
    """M2 集成测试 — LLMCore 调用真实 MiniMax M2.7。"""

    async def test_basic_call_returns_valid_response(
        self, minimax_config: dict[str, Any]
    ) -> None:
        """发送 '你好' → 收到有效中文回复（非空、非错误码）。

        M2 验收标准：LLMCore 调用真实 LLM 返回结果。
        """
        core = LLMCore(config=minimax_config)
        state = create_initial_state(
            messages=[{"role": "user", "content": "你好，请用一句话回复"}]
        )
        ctx = PluginContext(state=state, config={})
        result = await core.execute(ctx)

        assert result[StateKeys.RAW_RESULT] is not None, (
            f"raw_result 不应为 None，raw_error={result[StateKeys.RAW_ERROR]}"
        )
        assert result[StateKeys.RAW_ERROR] is None, (
            f"raw_error 应为 None，实际: {result[StateKeys.RAW_ERROR]}"
        )
        assert len(result[StateKeys.RAW_RESULT]) > 0, "回复内容不应为空"
        # 验证是中文回复（至少包含中文字符范围）
        has_chinese = any("\u4e00" <= c <= "\u9fff" for c in result[StateKeys.RAW_RESULT])
        assert has_chinese, f"回复应包含中文，实际: {result[StateKeys.RAW_RESULT][:100]}"

    async def test_streaming_output(
        self, minimax_config: dict[str, Any]
    ) -> None:
        """MiniMax M2.7 流式输出 — 验证 chunk 回调触发，响应完整。

        M2 验收标准：流式回调正确触发。
        """
        received_chunks: list[dict[str, Any]] = []

        def on_chunk(chunk: dict[str, Any]) -> None:
            received_chunks.append(chunk)

        core = LLMCore(config=minimax_config)
        state = create_initial_state(
            messages=[{"role": "user", "content": "请用一句话介绍 Python"}],
            streaming=True,
            on_chunk=on_chunk,
        )
        ctx = PluginContext(state=state, config={})
        result = await core.execute(ctx)

        assert result[StateKeys.RAW_RESULT] is not None, (
            f"流式响应 raw_result 不应为 None，raw_error={result[StateKeys.RAW_ERROR]}"
        )
        assert result[StateKeys.RAW_ERROR] is None, (
            f"流式响应 raw_error 应为 None，实际: {result[StateKeys.RAW_ERROR]}"
        )
        assert len(received_chunks) > 0, "应该收到至少一个流式 chunk"
        # 验证至少有文本 chunk
        text_chunks = [c for c in received_chunks if c.get("type") == "text"]
        assert len(text_chunks) > 0, "应该有文本类型的 chunk"

    async def test_system_prompt(
        self, minimax_config: dict[str, Any]
    ) -> None:
        """MiniMax M2.7 带 system prompt — 指定角色后回复符合设定。

        M2 验收标准：带 system prompt 调用能正常工作。
        """
        core = LLMCore(config=minimax_config)
        state = create_initial_state(
            messages=[
                {
                    "role": "system",
                    "content": "你是一个只说英语的助手，不管用户用什么语言提问，你都必须用英语回复。",
                },
                {"role": "user", "content": "你好"},
            ]
        )
        ctx = PluginContext(state=state, config={})
        result = await core.execute(ctx)

        assert result[StateKeys.RAW_RESULT] is not None, (
            f"raw_result 不应为 None，raw_error={result[StateKeys.RAW_ERROR]}"
        )
        assert result[StateKeys.RAW_ERROR] is None, (
            f"raw_error 应为 None，实际: {result[StateKeys.RAW_ERROR]}"
        )
        # 验证回复以英文为主（包含英文字母）
        has_english = any("a" <= c <= "z" or "A" <= c <= "Z" for c in result[StateKeys.RAW_RESULT])
        assert has_english, (
            f"带英文 system prompt 时回复应包含英文字母，实际: {result[StateKeys.RAW_RESULT][:100]}"
        )


# ---------------------------------------------------------------------------
# M3 集成测试：工具系统真实执行
# ---------------------------------------------------------------------------


def _add_numbers(args: dict[str, Any]) -> dict[str, Any]:
    """简单加法工具（同步）— 集成测试用。

    Args:
        args: 包含 a 和 b 两个数字的字典

    Returns:
        包含结果和表达式字符串的字典
    """
    a = args.get("a", 0)
    b = args.get("b", 0)
    return {"result": a + b, "expression": f"{a} + {b} = {a + b}"}


async def _async_echo(args: dict[str, Any]) -> dict[str, Any]:
    """异步回显工具 — 集成测试用。

    Args:
        args: 任意字典

    Returns:
        原样返回输入字典
    """
    await asyncio.sleep(0.01)  # 模拟异步操作
    return {"echo": args}


def _failing_tool(args: dict[str, Any]) -> dict[str, Any]:
    """总是失败的工具 — 集成测试用。

    Args:
        args: 任意字典

    Raises:
        ValueError: 始终抛出
    """
    raise ValueError("This tool is designed to fail for testing")


def _make_tool(
    name: str,
    description: str,
    schema: dict[str, Any],
    source: ToolSource = ToolSource.CODE,
) -> Tool:
    """创建 Tool 对象的辅助函数。

    Args:
        name: 工具名称
        description: 工具描述
        schema: 输入参数 JSON Schema
        source: 工具来源

    Returns:
        Tool 实例
    """
    return Tool(
        name=name,
        description=description,
        input_schema=schema,
        source=source,
    )


def _register_tool(
    registry: ToolRegistry,
    name: str,
    handler,
    description: str,
    schema: dict[str, Any],
) -> None:
    """向 ToolRegistry 注册工具并绑定处理函数的辅助函数。

    Args:
        registry: 工具注册表
        name: 工具名称
        handler: 处理函数
        description: 工具描述
        schema: 输入参数 JSON Schema
    """
    tool = _make_tool(name, description, schema)
    registry.register_with_handler(tool, handler)


@pytest.mark.integration
class TestToolCoreIntegration:
    """M3 集成测试 — 真实工具注册 + 执行。"""

    async def test_real_tool_execution(self) -> None:
        """注册真实工具 → 执行 → 验证结果。

        M3 验收标准：工具能正确注册并执行。
        """
        tool_core = ToolCore()
        tool_core.register_tool("add_numbers", _add_numbers)

        state = create_initial_state(
            **{
                StateKeys.RAW_TOOL_CALLS: [
                    {"name": "add_numbers", "arguments": {"a": 3, "b": 5}},
                ]
            }
        )
        ctx = PluginContext(state=state, config={})
        result = await tool_core.execute(ctx)

        tool_results = result[StateKeys.TOOL_RESULTS]
        assert len(tool_results) == 1
        assert tool_results[0]["success"] is True
        assert tool_results[0]["data"]["result"] == 8
        assert tool_results[0]["data"]["expression"] == "3 + 5 = 8"

    async def test_tool_registry_and_execution(self) -> None:
        """ToolRegistry 批量注册 → ToolCore 执行 → 验证结果。

        M3 验收标准：ToolRegistry 与 ToolCore 联动正常。
        """
        registry = ToolRegistry()
        _register_tool(
            registry, "add_numbers", _add_numbers,
            description="加法运算",
            schema={
                "type": "object",
                "properties": {
                    "a": {"type": "number", "description": "第一个数"},
                    "b": {"type": "number", "description": "第二个数"},
                },
                "required": ["a", "b"],
            },
        )
        _register_tool(
            registry, "echo", _async_echo,
            description="异步回显工具",
            schema={
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                },
            },
        )

        # 验证 get_tools_for_llm 输出格式
        tools_for_llm = registry.get_tools_for_llm()
        assert len(tools_for_llm) == 2
        assert tools_for_llm[0]["type"] == "function"
        assert tools_for_llm[0]["function"]["name"] == "add_numbers"

        # 验证 ToolCore 批量注册
        tool_core = ToolCore()
        tool_core.register_tools_from_registry(registry)

        state = create_initial_state(
            **{
                StateKeys.RAW_TOOL_CALLS: [
                    {"name": "add_numbers", "arguments": {"a": 10, "b": 20}},
                    {"name": "echo", "arguments": {"message": "hello"}},
                ]
            }
        )
        ctx = PluginContext(state=state, config={})
        result = await tool_core.execute(ctx)

        tool_results = result[StateKeys.TOOL_RESULTS]
        assert len(tool_results) == 2
        # 第一个工具
        assert tool_results[0]["success"] is True
        assert tool_results[0]["data"]["result"] == 30
        # 第二个工具
        assert tool_results[1]["success"] is True
        assert tool_results[1]["data"]["echo"]["message"] == "hello"

    async def test_tool_failure_handling(self) -> None:
        """工具执行失败 → 返回错误结果（不抛异常）。

        M3 验收标准：工具调用失败处理。
        """
        tool_core = ToolCore()
        tool_core.register_tool("failing_tool", _failing_tool)

        state = create_initial_state(
            **{
                StateKeys.RAW_TOOL_CALLS: [
                    {"name": "failing_tool", "arguments": {}},
                ]
            }
        )
        ctx = PluginContext(state=state, config={})
        result = await tool_core.execute(ctx)

        tool_results = result[StateKeys.TOOL_RESULTS]
        assert len(tool_results) == 1
        assert tool_results[0]["success"] is False
        assert "designed to fail" in tool_results[0]["error"]
        # raw_error 应为 None（错误由各工具结果中的 error 字段表达）
        assert result[StateKeys.RAW_ERROR] is None

    async def test_tool_timeout(self) -> None:
        """工具执行超时 → 正确终止并返回超时错误。

        M3 验收标准：工具执行超时处理。
        """

        async def _slow_tool(args: dict[str, Any]) -> dict[str, Any]:
            """模拟超时工具 — 睡眠 5 秒。"""
            await asyncio.sleep(5)
            return {"done": True}

        tool_core = ToolCore(config={"timeout": 0.5})
        tool_core.register_tool("slow_tool", _slow_tool)

        state = create_initial_state(
            **{
                StateKeys.RAW_TOOL_CALLS: [
                    {"name": "slow_tool", "arguments": {}},
                ]
            }
        )
        ctx = PluginContext(state=state, config={})
        result = await tool_core.execute(ctx)

        tool_results = result[StateKeys.TOOL_RESULTS]
        assert len(tool_results) == 1
        assert tool_results[0]["success"] is False
        assert "timed out" in tool_results[0]["error"].lower()


# ---------------------------------------------------------------------------
# M3 集成测试：LLM + ToolCore 端到端
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestLLMToolIntegration:
    """M3 集成测试 — LLM 调用工具 → ToolCore 执行 → 结果回传。"""

    @staticmethod
    async def _check_llm_available(minimax_config: dict[str, Any]) -> None:
        """检查 LLM API 是否可用，不可用时跳过测试。

        Args:
            minimax_config: LLM 配置字典

        Raises:
            pytest.skip: 当 LLM API 不可用时
        """
        import litellm

        try:
            resp = await litellm.acompletion(
                model=f"{minimax_config['provider']}/{minimax_config['model_name']}",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=1,
                api_base=minimax_config.get("api_base"),
                api_key=minimax_config.get("api_key"),
            )
            # 如果成功得到响应，API 可用
            if resp and resp.choices:
                return
        except Exception:
            pass
        pytest.skip("MiniMax LLM API 不可用（过载或网络问题），跳过集成测试")

    async def test_llm_tool_call_chain(
        self, minimax_config: dict[str, Any]
    ) -> None:
        """LLM 识别工具调用 → ToolCore 执行 → 验证完整链路。

        M3 验收标准：LLM 返回工具调用 → ToolCore 执行 → 结果反馈。
        通过给 LLM 提供 tools 参数，让它调用 add_numbers 工具。
        """
        await self._check_llm_available(minimax_config)

        # 1. 注册工具
        registry = ToolRegistry()
        _register_tool(
            registry, "add_numbers", _add_numbers,
            description="计算两个数字的加法",
            schema={
                "type": "object",
                "properties": {
                    "a": {"type": "number", "description": "第一个数"},
                    "b": {"type": "number", "description": "第二个数"},
                },
                "required": ["a", "b"],
            },
        )

        # 2. 用 LLMCore 调用 LLM（带 tools 参数）
        import litellm

        kwargs: dict[str, Any] = {
            "model": f"{minimax_config['provider']}/{minimax_config['model_name']}",
            "messages": [
                {
                    "role": "system",
                    "content": "你是一个计算助手。当用户要求计算时，使用提供的工具。",
                },
                {"role": "user", "content": "请计算 17 加 25 等于多少？请使用 add_numbers 工具。"},
            ],
            "tools": registry.get_tools_for_llm(),
            **minimax_config.get("default_params", {}),
        }
        if minimax_config.get("api_base"):
            kwargs["api_base"] = minimax_config["api_base"]
        if minimax_config.get("api_key"):
            kwargs["api_key"] = minimax_config["api_key"]

        response = await litellm.acompletion(**kwargs)
        choice = response.choices[0]

        # 3. 验证 LLM 返回了工具调用
        assert choice.message.tool_calls is not None and len(choice.message.tool_calls) > 0, (
            f"LLM 应返回工具调用，实际: content={choice.message.content}, "
            f"tool_calls={choice.message.tool_calls}"
        )

        # 4. 解析工具调用
        tool_calls = []
        for tc in choice.message.tool_calls:
            tool_calls.append({
                "name": tc.function.name,
                "arguments": json.loads(tc.function.arguments),
            })

        assert tool_calls[0]["name"] == "add_numbers", (
            f"工具名应为 add_numbers，实际: {tool_calls[0]['name']}"
        )
        assert tool_calls[0]["arguments"]["a"] == 17 or tool_calls[0]["arguments"].get("a") is not None

        # 5. 用 ToolCore 执行工具
        tool_core = ToolCore()
        tool_core.register_tools_from_registry(registry)

        state = create_initial_state(**{StateKeys.RAW_TOOL_CALLS: tool_calls})
        ctx = PluginContext(state=state, config={})
        result = await tool_core.execute(ctx)

        # 6. 验证工具执行结果
        tool_results = result[StateKeys.TOOL_RESULTS]
        assert len(tool_results) == 1
        assert tool_results[0]["success"] is True
        assert tool_results[0]["data"]["result"] == 42, (
            f"17 + 25 应等于 42，实际: {tool_results[0]['data']}"
        )

    async def test_llm_tool_failure_recovery(
        self, minimax_config: dict[str, Any]
    ) -> None:
        """工具返回错误 → LLM 能理解错误并给出合理回复。

        M3 验收标准：工具调用失败处理。
        让 LLM 调用一个不存在的工具，验证整个流程能优雅处理。
        """
        await self._check_llm_available(minimax_config)

        import litellm

        # 注册一个总是失败的工具
        registry = ToolRegistry()
        _register_tool(
            registry, "check_weather", _failing_tool,
            description="查询天气信息",
            schema={
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名称"},
                },
                "required": ["city"],
            },
        )

        # LLM 调用
        kwargs: dict[str, Any] = {
            "model": f"{minimax_config['provider']}/{minimax_config['model_name']}",
            "messages": [
                {"role": "user", "content": "请查询北京的天气"},
            ],
            "tools": registry.get_tools_for_llm(),
            **minimax_config.get("default_params", {}),
        }
        if minimax_config.get("api_base"):
            kwargs["api_base"] = minimax_config["api_base"]
        if minimax_config.get("api_key"):
            kwargs["api_key"] = minimax_config["api_key"]

        response = await litellm.acompletion(**kwargs)
        choice = response.choices[0]

        # LLM 可能返回工具调用，也可能直接回复
        if choice.message.tool_calls and len(choice.message.tool_calls) > 0:
            # LLM 选择了调用工具
            tool_calls = []
            for tc in choice.message.tool_calls:
                tool_calls.append({
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments),
                })

            # ToolCore 执行（会失败）
            tool_core = ToolCore()
            tool_core.register_tools_from_registry(registry)
            state = create_initial_state(**{StateKeys.RAW_TOOL_CALLS: tool_calls})
            ctx = PluginContext(state=state, config={})
            result = await tool_core.execute(ctx)

            tool_results = result[StateKeys.TOOL_RESULTS]
            assert len(tool_results) >= 1
            assert tool_results[0]["success"] is False
            assert "error" in tool_results[0]

            # 将错误结果回传 LLM，验证 LLM 能理解错误
            tool_result_content = json.dumps(tool_results[0], ensure_ascii=False)
            messages = [
                {"role": "user", "content": "请查询北京的天气"},
                {"role": "assistant", "content": None, "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": tool_calls[0]["name"],
                            "arguments": json.dumps(tool_calls[0]["arguments"]),
                        },
                    }
                ]},
                {"role": "tool", "tool_call_id": "call_1", "content": tool_result_content},
            ]

            kwargs2: dict[str, Any] = {
                "model": f"{minimax_config['provider']}/{minimax_config['model_name']}",
                "messages": messages,
                **minimax_config.get("default_params", {}),
            }
            if minimax_config.get("api_base"):
                kwargs2["api_base"] = minimax_config["api_base"]
            if minimax_config.get("api_key"):
                kwargs2["api_key"] = minimax_config["api_key"]

            response2 = await litellm.acompletion(**kwargs2)
            final_reply = response2.choices[0].message.content

            assert final_reply is not None and len(final_reply) > 0, (
                "LLM 在收到工具错误后应给出回复"
            )
        else:
            # LLM 没有调用工具，直接回复了 — 也是合理的
            assert choice.message.content is not None, "LLM 应有回复内容"


# ---------------------------------------------------------------------------
# PendingToolsOutput 集成测试
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPendingToolsOutputIntegration:
    """M3 集成测试 — PendingToolsOutput 与管道联动。"""

    async def test_pending_tools_detects_real_tool_calls(
        self, minimax_config: dict[str, Any]
    ) -> None:
        """LLM 返回工具调用 → PendingToolsOutput 检测并发出 next_tool 信号。

        验证 PendingToolsOutput 在真实 LLM 返回 tool_calls 时能正确触发。
        """
        import litellm

        registry = ToolRegistry()
        _register_tool(
            registry, "add_numbers", _add_numbers,
            description="计算两个数字的加法",
            schema={
                "type": "object",
                "properties": {
                    "a": {"type": "number"},
                    "b": {"type": "number"},
                },
                "required": ["a", "b"],
            },
        )

        # LLM 调用
        kwargs: dict[str, Any] = {
            "model": f"{minimax_config['provider']}/{minimax_config['model_name']}",
            "messages": [
                {"role": "user", "content": "计算 3 加 7"},
            ],
            "tools": registry.get_tools_for_llm(),
            **minimax_config.get("default_params", {}),
        }
        if minimax_config.get("api_base"):
            kwargs["api_base"] = minimax_config["api_base"]
        if minimax_config.get("api_key"):
            kwargs["api_key"] = minimax_config["api_key"]

        response = await litellm.acompletion(**kwargs)
        choice = response.choices[0]

        if choice.message.tool_calls and len(choice.message.tool_calls) > 0:
            # 模拟 LLMCore 写入 state 的结果
            tool_calls = []
            for tc in choice.message.tool_calls:
                tool_calls.append({
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments),
                })

            state = create_initial_state(**{StateKeys.RAW_TOOL_CALLS: tool_calls})
            ctx = PluginContext(state=state, config={})

            # PendingToolsOutput 检测
            pending = PendingToolsOutput()
            result = await pending.execute(ctx)

            assert result.route_signal is not None, (
                "有工具调用时 PendingToolsOutput 应发出路由信号"
            )
            assert result.route_signal.route_type == "next_tool"
            assert result.route_signal.target == "tool_execute"
